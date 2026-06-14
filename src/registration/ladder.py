"""Self-recovering SMS recipe ladder for Instagram registration.

The recurring wall is SMS *delivery*: Instagram accepts a number and reaches the
6-digit code screen, but the cheap VOIP pools (e.g. SMSPool Vietnam) never
receive the code. One number source is therefore not enough — we need to keep
trying with *different approaches* until one delivers.

``RecipeLadder`` is the outer loop that does exactly that. It runs a
``RegistrationRunner`` (the existing single-attempt orchestration) once per
**recipe** — a ``(provider, country, operator, max_price, sms_timeout)`` tuple —
and after each attempt classifies the outcome:

  * success                  -> STOP, return the winning result.
  * fatal failure            -> STOP (a different *number* cannot help):
                                device unreachable, the shared-IP rate-limit,
                                account suspended, operator told us to abort.
  * recoverable failure      -> advance to the NEXT recipe. The runner restores
                                the golden clean backup at the start of every
                                attempt, so each recipe gets a fresh, account-free
                                Instagram — no manual reset between tries.

The ladder is deliberately decoupled from the device: it does not rotate the
device identity itself (that is a heavy, reboot-and-reconnect step the operator
drives once up front on the Tailscale test setup, or the per-runner ``rotator``
handles in production on the LAN). It only varies the *number source* and lets
the runner restore the clean backup between tries.

Everything is injectable (``runner_factory``) so the loop is unit-testable
without a device, a network, or spending money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.genfarmer.app_backup import AppBackupClient
from src.registration.result import RegistrationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipe:
    """One number-sourcing approach the ladder can try.

    ``provider`` selects the SMS client (``5sim`` or ``smspool``). ``operator``
    is a 5sim operator priority list (e.g. ``"virtual51,any"``); ignored by
    SMSPool. ``max_price`` raises 5sim's per-number price cap so pricier
    high-delivery operators become buyable.
    """

    provider: str
    country: str
    operator: str = "any"
    max_price: float | None = None
    sms_timeout: float = 180.0
    label: str = ""

    def describe(self) -> str:
        if self.label:
            return self.label
        bits = [self.provider, self.country]
        if self.operator and self.operator != "any":
            bits.append(self.operator)
        if self.max_price is not None:
            bits.append(f"<=${self.max_price}")
        return "/".join(bits)


# Default ladder, ordered by what we actually know about IG-acceptance AND 5sim
# delivery (see the autoreg-status memory). austria/virtual51 is the ONE recipe
# that has produced a real, logged-in account end to end, so it leads. The rest
# are high-delivery 5sim operators (unlocked with max_price) followed by a
# real-SIM SMSPool fallback. Tune freely via --recipes <json>.
DEFAULT_LADDER: tuple[Recipe, ...] = (
    Recipe("5sim", "austria", "virtual51,any", 0.60, 150.0, "5sim austria/virtual51 (proven)"),
    Recipe("5sim", "luxembourg", "any", 0.60, 150.0, "5sim luxembourg"),
    Recipe("5sim", "croatia", "virtual4,any", 0.60, 150.0, "5sim croatia/virtual4"),
    Recipe("5sim", "czech", "virtual34,any", 0.60, 150.0, "5sim czech/virtual34"),
    Recipe("smspool", "usa", "any", None, 180.0, "smspool USA (real-SIM)"),
)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

# A different *number* cannot fix these, so the ladder stops immediately rather
# than burning money/risk on more numbers:
#   device_unreachable     - the phone fell off ADB; fix connectivity first.
#   rate_limited           - Instagram is throttling this device/egress IP; a
#                            new number won't clear it (needs a proxy / new IP /
#                            device rotation, which the ladder does not do).
#   account_suspended      - IG killed the account on creation.
#   operator_abort         - a human told the agent to stop.
DEFAULT_FATAL_REASONS: frozenset[str] = frozenset(
    {
        "device_unreachable",
        "rate_limited",
        "account_suspended",
        "operator_abort",
        "signup_blocked",
    }
)

# Number-source problems — exactly what the next recipe is meant to fix:
DEFAULT_RECOVERABLE_REASONS: frozenset[str] = frozenset(
    {
        "phone_verification_failed",
        "no_structured_output",
        "clean_backup_requested_but_no_backup_client",
    }
)

STOP_SUCCESS = "success"
STOP_FATAL = "fatal"
CONTINUE = "continue"


def _reason_base(reason: str | None) -> str:
    """First token of a failure_reason (``agent_exception: foo`` -> ``agent_exception``)."""
    return (reason or "").split(":", 1)[0].strip()


def classify(
    result: RegistrationResult,
    *,
    fatal_reasons: frozenset[str] = DEFAULT_FATAL_REASONS,
    recoverable_reasons: frozenset[str] = DEFAULT_RECOVERABLE_REASONS,
    retry_unknown: bool = True,
) -> str:
    """Decide what the ladder should do after one attempt.

    Returns ``STOP_SUCCESS``, ``STOP_FATAL``, or ``CONTINUE``.

    ``retry_unknown`` controls the policy for failure reasons we don't recognise
    (and transient ``agent_exception: ...`` crashes): when True (default) we keep
    trying the next recipe — favouring resilience — bounded by the recipe count.
    """
    if result.success:
        return STOP_SUCCESS
    base = _reason_base(result.failure_reason)
    if base in fatal_reasons:
        return STOP_FATAL
    if base in recoverable_reasons or base == "agent_exception":
        return CONTINUE
    return CONTINUE if retry_unknown else STOP_FATAL


# ---------------------------------------------------------------------------
# Ladder result
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    """Record of one recipe attempt."""

    index: int
    recipe: Recipe
    result: RegistrationResult
    decision: str

    def summary(self) -> str:
        r = self.result
        tag = "OK" if r.success else (r.failure_reason or "failed")
        return f"#{self.index} [{self.recipe.describe()}] -> {tag} ({self.decision})"


@dataclass
class LadderResult:
    """Outcome of the whole ladder run."""

    success: bool
    final: RegistrationResult | None
    attempts: list[Attempt] = field(default_factory=list)
    stopped_reason: str = ""  # success | fatal | exhausted | max_attempts

    @property
    def winning_recipe(self) -> Recipe | None:
        for a in self.attempts:
            if a.result.success:
                return a.recipe
        return None


# ---------------------------------------------------------------------------
# Runner protocol + ladder
# ---------------------------------------------------------------------------

# A "runner" is anything with ``async run() -> RegistrationResult`` — in
# production this is ``RegistrationRunner`` built for a specific recipe.
Runner = Any
RunnerFactory = Callable[[Recipe, int], Runner]


class RecipeLadder:
    """Run registration attempts across recipes until one succeeds (or we stop)."""

    def __init__(
        self,
        recipes: list[Recipe] | tuple[Recipe, ...],
        runner_factory: RunnerFactory,
        *,
        max_attempts: int | None = None,
        fatal_reasons: frozenset[str] = DEFAULT_FATAL_REASONS,
        recoverable_reasons: frozenset[str] = DEFAULT_RECOVERABLE_REASONS,
        retry_unknown: bool = True,
    ) -> None:
        if not recipes:
            raise ValueError("recipes must be non-empty")
        self._recipes = list(recipes)
        self._runner_factory = runner_factory
        self._max_attempts = max_attempts
        self._fatal_reasons = fatal_reasons
        self._recoverable_reasons = recoverable_reasons
        self._retry_unknown = retry_unknown

    async def run(self) -> LadderResult:
        attempts: list[Attempt] = []
        limit = len(self._recipes)
        if self._max_attempts is not None:
            limit = min(limit, self._max_attempts)

        for index in range(limit):
            recipe = self._recipes[index]
            logger.info(
                "Ladder attempt %d/%d: %s", index + 1, limit, recipe.describe()
            )
            runner = self._runner_factory(recipe, index)
            try:
                result = await runner.run()
            except Exception as exc:  # never let one attempt kill the ladder
                logger.exception("ladder runner raised on recipe %s", recipe.describe())
                result = RegistrationResult(
                    success=False, failure_reason=f"agent_exception: {exc}"
                )
            decision = classify(
                result,
                fatal_reasons=self._fatal_reasons,
                recoverable_reasons=self._recoverable_reasons,
                retry_unknown=self._retry_unknown,
            )
            attempts.append(Attempt(index=index, recipe=recipe, result=result, decision=decision))
            logger.info("  -> %s", attempts[-1].summary())

            if decision == STOP_SUCCESS:
                return LadderResult(True, result, attempts, "success")
            if decision == STOP_FATAL:
                logger.warning(
                    "Ladder stopping early — fatal reason %r (a new number cannot help).",
                    result.failure_reason,
                )
                return LadderResult(False, result, attempts, "fatal")

        stopped = "max_attempts" if (self._max_attempts is not None and limit < len(self._recipes)) else "exhausted"
        final = attempts[-1].result if attempts else None
        logger.warning("Ladder exhausted %d recipe(s) without success (%s).", len(attempts), stopped)
        return LadderResult(False, final, attempts, stopped)


# ---------------------------------------------------------------------------
# Default runner factory (wires RegistrationRunner per recipe)
# ---------------------------------------------------------------------------


@dataclass
class DeviceConfig:
    """The device/run-level config that is CONSTANT across every recipe.

    Only the number-source (the ``Recipe``) varies between attempts; the device,
    the golden backup, the CSV, artifacts root, MobileRun config and the app
    backup client stay the same.
    """

    device_serial: str
    clean_backup_dir: str | Path | None = None
    apk_path: str | Path | None = None
    csv_path: str = "accounts.csv"
    config_path: str = "config/mobilerun/config.yaml"
    artifacts_dir: str = "artifacts/registration"
    backup_client: AppBackupClient | None = None
    genfarmer_id: str | None = None
    timeout_seconds: int = 1800
    rotator: Any | None = None  # NoopRotator on the Tailscale test setup
    sms_client_factory: Callable[[str], Any] | None = None
    snapshot_fn: Callable[..., Awaitable[Any]] | None = None
    agent_factory: Callable[[Any], Any] | None = None


def default_runner_factory(cfg: DeviceConfig) -> RunnerFactory:
    """Build a ``RunnerFactory`` that constructs a ``RegistrationRunner`` per recipe.

    A fresh SMS client is created for each recipe's provider, and the artifacts
    dir is namespaced per attempt so each try keeps its own trajectory/screens.
    """
    from src.registration.cli import RegistrationRunner, _make_sms_client

    make_client = cfg.sms_client_factory or _make_sms_client

    def factory(recipe: Recipe, index: int) -> Runner:
        return RegistrationRunner(
            device_serial=cfg.device_serial,
            apk_path=cfg.apk_path,
            clean_backup_dir=cfg.clean_backup_dir,
            csv_path=cfg.csv_path,
            country=recipe.country,
            five_sim_client=make_client(recipe.provider),
            operator=recipe.operator,
            max_price=recipe.max_price,
            sms_timeout_seconds=recipe.sms_timeout,
            config_path=cfg.config_path,
            timeout_seconds=cfg.timeout_seconds,
            artifacts_dir=cfg.artifacts_dir,
            backup_client=cfg.backup_client,
            device_genfarmer_id=cfg.genfarmer_id,
            rotator=cfg.rotator,
            snapshot_fn=cfg.snapshot_fn,
            agent_factory=cfg.agent_factory,
        )

    return factory


def load_recipes(path: str | Path) -> list[Recipe]:
    """Load a recipe ladder from a JSON file (list of objects).

    Each object: ``{"provider","country","operator"?,"max_price"?,"sms_timeout"?,"label"?}``.
    """
    import json

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("recipes JSON must be a list of recipe objects")
    out: list[Recipe] = []
    for item in data:
        out.append(
            Recipe(
                provider=str(item["provider"]),
                country=str(item["country"]),
                operator=str(item.get("operator", "any")),
                max_price=item.get("max_price"),
                sms_timeout=float(item.get("sms_timeout", 180.0)),
                label=str(item.get("label", "")),
            )
        )
    if not out:
        raise ValueError("recipes JSON is empty")
    return out


__all__ = [
    "Recipe",
    "DEFAULT_LADDER",
    "classify",
    "STOP_SUCCESS",
    "STOP_FATAL",
    "CONTINUE",
    "Attempt",
    "LadderResult",
    "RecipeLadder",
    "DeviceConfig",
    "default_runner_factory",
    "load_recipes",
]
