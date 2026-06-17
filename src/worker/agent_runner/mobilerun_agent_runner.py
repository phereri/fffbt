"""``MobileRunAgentRunner`` — primary proof_of_posting executor.

Builds and runs a Mobilerun ``MobileAgent`` for one device, with the
Instagram AppCard providing the playbook. The runner is constructed by
``MobileUIAutomationStep`` when ``MOBILE_UI_EXECUTOR=mobilerun_agent``
(the default).

Design notes:
- ``mobilerun`` and ``pydantic`` are imported lazily inside
  ``_default_agent_factory`` so unit tests can patch the factory without
  installing those packages.
- The runner accepts an injectable ``agent_factory`` callable. Real runs use
  the default factory which loads ``config/mobilerun/config.yaml``, applies
  per-device serial + TCP control, merges per-platform defaults, and
  constructs ``MobileAgent`` with a Pydantic ``PostResult`` output model.
- Trajectory artifacts are captured by snapshotting the trajectories
  directory before and after the run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.worker.agent_runner.goal import build_trial_reel_goal
from src.worker.agent_runner.result import (
    AgentPostResult,
    AgentRunnerResult,
    ResultCategory,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure → error_code mapping
# ---------------------------------------------------------------------------


_FAILURE_REASON_MAP: dict[str, tuple[str, ResultCategory]] = {
    # Hard-stop conditions (FAILED, account-side-effects in error_catalog).
    "logged_out": ("logged_out", ResultCategory.HARD_STOP),
    "login_challenge": ("login_challenge", ResultCategory.HARD_STOP),
    "two-factor": ("login_challenge", ResultCategory.HARD_STOP),
    "2fa": ("login_challenge", ResultCategory.HARD_STOP),
    "checkpoint": ("login_challenge", ResultCategory.HARD_STOP),
    "account_suspended": ("account_suspended", ResultCategory.HARD_STOP),
    "suspended": ("account_suspended", ResultCategory.HARD_STOP),
    "action_blocked": ("action_blocked", ResultCategory.HARD_STOP),
    "action blocked": ("action_blocked", ResultCategory.HARD_STOP),
    "trial_reels_unavailable": ("trial_reels_unavailable", ResultCategory.HARD_STOP),
    "unexpected_destructive_dialog": (
        "unexpected_destructive_dialog",
        ResultCategory.HARD_STOP,
    ),
    # Soft failures (NEEDS_REVIEW).
    "share_did_not_register": ("share_did_not_register", ResultCategory.NEEDS_REVIEW),
    "final_ok_did_not_register": (
        "final_ok_did_not_register",
        ResultCategory.NEEDS_REVIEW,
    ),
    "caption_mismatch": ("caption_mismatch", ResultCategory.NEEDS_REVIEW),
    "caption_no_match": ("caption_mismatch", ResultCategory.NEEDS_REVIEW),
    "trial_toggle_off": ("unknown_screen", ResultCategory.NEEDS_REVIEW),
    "share_screen_not_reached": (
        "share_screen_not_reached",
        ResultCategory.NEEDS_REVIEW,
    ),
    "editor_next_not_reached": (
        "editor_next_not_reached",
        ResultCategory.NEEDS_REVIEW,
    ),
    "trial_reels_gallery_not_reached": (
        "trial_reels_gallery_not_reached",
        ResultCategory.NEEDS_REVIEW,
    ),
}


def map_failure_reason(reason: str | None) -> tuple[str, ResultCategory]:
    """Map an agent-returned failure_reason to (error_code, category).

    Unknown reasons map to ``unknown_screen`` / ``NEEDS_REVIEW``.
    """
    if not reason:
        return ("unknown_screen", ResultCategory.NEEDS_REVIEW)
    lowered = reason.lower().strip()
    if lowered in _FAILURE_REASON_MAP:
        return _FAILURE_REASON_MAP[lowered]
    for key, mapped in _FAILURE_REASON_MAP.items():
        if key in lowered:
            return mapped
    return ("unknown_screen", ResultCategory.NEEDS_REVIEW)


# ---------------------------------------------------------------------------
# Agent factory contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentFactoryRequest:
    """Inputs passed to an agent factory.

    Kept as a frozen dataclass so tests can construct it and inspect the
    exact request the runner produced without depending on mobilerun.
    """

    goal: str
    device_serial: str
    variables: dict[str, Any]
    overrides: dict[str, Any]
    config_path: str
    platform: str
    timeout_seconds: int
    tools: dict[str, Any] | tuple[Any, ...] = ()
    output_model: Any | None = None


class _AgentHandle:
    """Minimal interface the runner needs from the agent.

    Real ``MobileAgent`` already exposes ``.run()`` returning an awaitable
    ``ResultEvent``. Tests can substitute any object with this shape.
    """

    async def run(self) -> Any:  # pragma: no cover - protocol shape
        raise NotImplementedError


AgentFactory = Callable[[AgentFactoryRequest], _AgentHandle]


# ---------------------------------------------------------------------------
# MobileRunAgentRunner
# ---------------------------------------------------------------------------


_DEFAULT_CONFIG_PATH = "config/mobilerun/config.yaml"
_DEFAULT_PLATFORM = "instagram"
_DEFAULT_TIMEOUT_SECONDS = 1500


class MobileRunAgentRunner:
    """Build and run a MobileRun ``MobileAgent`` for one device, one job."""

    def __init__(
        self,
        *,
        device_serial: str,
        job_id: str,
        caption: str,
        hashtags: list[str] | None = None,
        expected_username: str | None = None,
        video_id: str | None = None,
        local_video_path: str | Path | None = None,
        host_video_in_gallery: str | None = None,
        mode: str = "proof_of_posting",
        config_path: str | Path | None = None,
        app_cards_dir: str | Path | None = None,
        trajectories_dir: str | Path | None = None,
        model_overrides: dict[str, Any] | None = None,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        agent_factory: AgentFactory | None = None,
        preferred_path: str | None = None,
    ) -> None:
        if mode != "proof_of_posting":
            raise ValueError(
                f"MobileRunAgentRunner only supports mode=proof_of_posting, got {mode!r}"
            )
        if not device_serial:
            raise ValueError("device_serial is required")
        if not caption:
            raise ValueError("caption is required")

        self._device_serial = device_serial
        self._job_id = job_id
        self._caption = caption
        self._hashtags = list(hashtags or [])
        self._expected_username = expected_username
        self._video_id = video_id
        self._local_video_path = local_video_path
        self._host_video_in_gallery = host_video_in_gallery
        self._mode = mode
        self._timeout_seconds = int(timeout_seconds)
        self._preferred_path = preferred_path

        self._config_path = str(
            config_path
            or os.environ.get("MOBILERUN_CONFIG")
            or _DEFAULT_CONFIG_PATH
        )
        self._app_cards_dir = str(app_cards_dir) if app_cards_dir else None
        self._trajectories_dir = str(
            trajectories_dir
            or os.environ.get("MOBILERUN_TRAJECTORIES_DIR")
            or "trajectories"
        )
        self._model_overrides = dict(model_overrides or {})
        self._agent_factory: AgentFactory = agent_factory or _default_agent_factory

    @property
    def device_serial(self) -> str:
        return self._device_serial

    @property
    def job_id(self) -> str:
        return self._job_id

    def build_request(self) -> AgentFactoryRequest:
        """Materialize the ``AgentFactoryRequest`` for the current job."""
        goal = build_trial_reel_goal(
            device_serial=self._device_serial,
            caption=self._caption,
            hashtags=self._hashtags,
            expected_username=self._expected_username,
            video_id=self._video_id,
            local_video_path=self._local_video_path,
            host_video_in_gallery=self._host_video_in_gallery,
            preferred_path=self._preferred_path,
        )

        # TCP control is mandatory for the agent path. Everything else either
        # comes from config.yaml or from the explicit model_overrides.
        overrides: dict[str, Any] = {"use_tcp": True}
        if self._app_cards_dir:
            overrides["app_cards_dir"] = self._app_cards_dir
        if self._trajectories_dir:
            overrides["trajectory_path"] = self._trajectories_dir
        for key, value in self._model_overrides.items():
            overrides[key] = value

        variables = {
            "device_serial": self._device_serial,
            "job_id": self._job_id,
            "video_id": self._video_id,
            "caption": self._caption,
            "hashtags": self._hashtags,
            "expected_username": self._expected_username,
            "host_video_in_gallery": self._host_video_in_gallery,
            "local_video_path": (
                str(self._local_video_path) if self._local_video_path else None
            ),
        }

        # Register the FFFBT custom Instagram tools (hide_ime,
        # tap_share_and_confirm, …) the goal + AppCard instruct the agent to
        # use. Without these the agent only has generic primitives and the
        # Mobilerun Keyboard swallows the Share tap (false share_did_not_register).
        from src.worker.agent_runner.custom_tools import build_instagram_custom_tools

        # Bind the SAME full caption (body + hashtags) the goal renders and the
        # agent pastes, so verify_caption_text compares against the on-screen text
        # rather than the body-only caption_base (which would false-fail).
        _hashtag_str = " ".join(
            f"#{h.lstrip('#')}" for h in self._hashtags if h.strip()
        )
        _caption_full = self._caption.rstrip()
        if _hashtag_str:
            _caption_full = f"{_caption_full}\n\n{_hashtag_str}"
        _caption_full = _caption_full.strip()

        custom_tools = build_instagram_custom_tools(
            serial=self._device_serial,
            video_id=self._video_id,
            caption=_caption_full,
        )

        return AgentFactoryRequest(
            goal=goal,
            device_serial=self._device_serial,
            variables=variables,
            overrides=overrides,
            config_path=self._config_path,
            platform=_DEFAULT_PLATFORM,
            timeout_seconds=self._timeout_seconds,
            tools=custom_tools,
        )

    async def run(self) -> AgentRunnerResult:
        """Build the agent, run it, and return a typed ``AgentRunnerResult``."""
        request = self.build_request()
        trajectories_before = _snapshot_trajectory_dir(self._trajectories_dir)
        agent = self._agent_factory(request)

        try:
            raw = await _await_agent_run(agent)
        except Exception as exc:
            logger.exception("MobileRunAgentRunner: agent.run raised")
            return AgentRunnerResult(
                category=ResultCategory.INFRA,
                success=False,
                error_code="UNKNOWN",
                failure_reason=f"agent_exception: {exc}",
                message=f"agent.run raised: {exc}",
                trajectory_paths=_new_trajectory_paths(
                    self._trajectories_dir, trajectories_before
                ),
            )

        trajectories_after = _new_trajectory_paths(
            self._trajectories_dir, trajectories_before
        )
        structured = AgentPostResult.from_structured(
            _attr(raw, "structured_output", None),
            device_serial=self._device_serial,
            video_id=self._video_id,
            caption=self._caption,
        )
        agent_status = _optional_str(_attr(raw, "status", None))

        if structured is not None and structured.success:
            return AgentRunnerResult(
                category=ResultCategory.OK,
                success=True,
                error_code=None,
                failure_reason=None,
                message="Trial Reel published via MobileRun agent",
                structured=structured,
                trajectory_paths=trajectories_after,
                agent_status=agent_status,
                raw_result=raw,
            )

        if structured is not None:
            reason = structured.failure_reason
        else:
            reason = _optional_str(_attr(raw, "reason", None))

        error_code, category = map_failure_reason(reason)
        return AgentRunnerResult(
            category=category,
            success=False,
            error_code=error_code,
            failure_reason=reason,
            message=f"agent reported failure_reason={reason!r}",
            structured=structured,
            trajectory_paths=trajectories_after,
            agent_status=agent_status,
            raw_result=raw,
        )


# ---------------------------------------------------------------------------
# Default factory (lazily imports mobilerun + pydantic)
# ---------------------------------------------------------------------------


def _default_agent_factory(request: AgentFactoryRequest) -> _AgentHandle:
    """Build a real Mobilerun ``MobileAgent`` for the request.

    Imports ``mobilerun`` and ``pydantic`` lazily so the surrounding module
    stays importable in environments where neither is installed (CI, Mac
    dev). Raises ``RuntimeError`` if mobilerun is missing at call time.
    """
    try:
        from mobilerun import MobileAgent, MobileConfig  # noqa: F401
        from mobilerun.config_manager import ConfigLoader
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "mobilerun package not installed — agent executor is unavailable. "
            "Install mobilerun on this host or run with MOBILE_UI_EXECUTOR=deterministic."
        ) from exc

    config = ConfigLoader().load(request.config_path)
    config.device.serial = request.device_serial
    if ":" in (request.device_serial or ""):
        config.device.use_tcp = True
    if "use_tcp" in request.overrides:
        config.device.use_tcp = bool(request.overrides["use_tcp"])
    if "trajectory_path" in request.overrides:
        try:
            config.logging.trajectory_path = str(request.overrides["trajectory_path"])
        except Exception:
            logger.debug("could not override trajectory_path on MobileConfig")
    if "app_cards_dir" in request.overrides:
        try:
            config.agent.app_cards.app_cards_dir = str(request.overrides["app_cards_dir"])
        except Exception:
            logger.debug("could not override app_cards_dir on MobileConfig")
    for key in ("max_steps", "manager_vision", "executor_vision", "stealth"):
        if key in request.overrides:
            try:
                if key == "max_steps":
                    config.agent.max_steps = int(request.overrides[key])
                elif key == "manager_vision":
                    config.agent.manager.vision = bool(request.overrides[key])
                elif key == "executor_vision":
                    config.agent.executor.vision = bool(request.overrides[key])
                elif key == "stealth":
                    config.tools.stealth = bool(request.overrides[key])
            except Exception:
                logger.debug("could not apply override %s", key)

    post_result_model = request.output_model or _post_result_pydantic_model()
    agent_kwargs: dict[str, Any] = dict(
        goal=request.goal,
        config=config,
        variables=request.variables,
        output_model=post_result_model,
        timeout=request.timeout_seconds,
    )
    if request.tools:
        # MobileRun expects custom tools as a dict:
        #   {name: {"function": callable, "parameters": {...}, "description": str}}
        # (see mobilerun.agent.tool_registry.register_from_dict). Accept either a
        # ready-made dict or a list of callables (wrapped by name).
        if isinstance(request.tools, dict):
            agent_kwargs["custom_tools"] = request.tools
        else:
            agent_kwargs["custom_tools"] = {
                getattr(fn, "__name__", f"tool_{i}"): {"function": fn}
                for i, fn in enumerate(request.tools)
            }
    return MobileAgent(**agent_kwargs)


# ---------------------------------------------------------------------------
# Generic in-process goal runner (used by the verification step)
# ---------------------------------------------------------------------------


def verification_result_model() -> type:
    """Structured-output model for a dashboard verification agent run."""
    from pydantic import BaseModel, Field

    class VerificationResult(BaseModel):
        success: bool = Field(
            description=(
                "True ONLY if a freshly posted Trial Reel was confirmed visible "
                "at the top of the Trial reels list; False otherwise."
            )
        )
        reason: str | None = Field(
            default=None, description="Short note on what was observed."
        )

    return VerificationResult


async def run_agent_goal(
    *,
    device_serial: str,
    goal: str,
    config_path: str | Path | None = None,
    app_cards_dir: str | Path | None = None,
    trajectories_dir: str | Path | None = None,
    model_overrides: dict[str, Any] | None = None,
    output_model: type | None = None,
    timeout_seconds: int = 200,
    agent_factory: AgentFactory | None = None,
) -> Any:
    """Run an arbitrary goal through the in-process ``MobileAgent`` — the same
    path the proof_of_posting executor uses — and return the agent's structured
    output object (or ``None``).

    The verification step uses this instead of ``MobilerunWorker.run_goal`` (the
    GenFarmer ``/automation/run`` endpoint), which returns without actually
    driving the device. No custom tools are registered: dashboard navigation
    only needs the stock primitives.
    """
    overrides: dict[str, Any] = {"use_tcp": True}
    if app_cards_dir:
        overrides["app_cards_dir"] = str(app_cards_dir)
    overrides["trajectory_path"] = str(
        trajectories_dir
        or os.environ.get("MOBILERUN_TRAJECTORIES_DIR")
        or "trajectories"
    )
    for key, value in (model_overrides or {}).items():
        overrides[key] = value

    request = AgentFactoryRequest(
        goal=goal,
        device_serial=device_serial,
        variables={"device_serial": device_serial},
        overrides=overrides,
        config_path=str(
            config_path or os.environ.get("MOBILERUN_CONFIG") or _DEFAULT_CONFIG_PATH
        ),
        platform=_DEFAULT_PLATFORM,
        timeout_seconds=int(timeout_seconds),
        output_model=output_model,
    )
    factory = agent_factory or _default_agent_factory
    agent = factory(request)
    raw = await _await_agent_run(agent)
    return _attr(raw, "structured_output", None)


def _post_result_pydantic_model() -> type:
    """Construct a Pydantic ``PostResult`` model — lazily, once per process."""
    from pydantic import BaseModel, Field

    class PostResult(BaseModel):
        success: bool = Field(description="True iff the Trial Reel was published.")
        platform: str = Field(default="instagram")
        device_serial: str = Field(description="Device id used.")
        account_username: str | None = Field(
            default=None,
            description=(
                "Best-effort: the active IG username, ONLY if read verbatim "
                "from the UI. Leave null if not certain — do not guess. "
                "Informational; not used to gate success (the agent is known "
                "to hallucinate this field)."
            ),
        )
        video_id: str | None = Field(default=None)
        caption: str | None = Field(default=None)
        post_url: str | None = Field(default=None)
        path_used: str | None = Field(
            default=None,
            description=(
                "The entry-path letter (A, B, or C) that reached the Trial Reel "
                "composer. Recorded so this account can try it first next time. "
                "Null if unsure."
            ),
        )
        failure_reason: str | None = Field(
            default=None,
            description="Machine-friendly reason when success is False.",
        )

    return PostResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _await_agent_run(agent: Any) -> Any:
    """Run an agent's ``run()`` method, handling sync + async returns."""
    method = getattr(agent, "run", None)
    if method is None:
        raise RuntimeError("agent has no run() method")
    result: Any = method()
    if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
        result = await result
    return result


def _snapshot_trajectory_dir(path: str | Path | None) -> set[str]:
    if not path:
        return set()
    base = Path(path)
    if not base.exists() or not base.is_dir():
        return set()
    try:
        return {str(p) for p in base.rglob("*") if p.is_file()}
    except OSError:
        return set()


def _new_trajectory_paths(path: str | Path | None, before: set[str]) -> list[str]:
    after = _snapshot_trajectory_dir(path)
    return sorted(after - before)


def _attr(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


# Re-exports for callers that only need the failure mapping.
__all__ = [
    "AgentFactoryRequest",
    "AgentFactory",
    "MobileRunAgentRunner",
    "map_failure_reason",
]


# Tiny helper used in tests to inspect mtime ordering deterministically.
def _touch(path: Path) -> None:  # pragma: no cover - test helper
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    os.utime(path, (time.time(), time.time()))
