"""CLI + orchestration for one Instagram account registration.

    python -m src.registration.cli register --device-serial <S> \
        [--country any] [--csv accounts.csv]

Orchestration per run (``RegistrationRunner.run``):
  1. ``rotate(serial)`` via the configured ``DeviceIdentityRotator`` (NoopRotator
     locally = capture-only) and verify the device is reachable.
  2. Snapshot the device fingerprint (always) + dump raw getprop to artifacts.
  3. Build a MobileRun agent with the registration goal + the custom tools
     (buy_phone_number / get_sms_code / ask_operator) and run it (ONE atomic run).
  4. Map the agent's structured output to ``RegistrationResult`` and append a row
     to the CSV with credentials + fingerprint + device serials.

Everything external (agent factory, fingerprint snapshot, rotator, 5sim client)
is injectable so the orchestration is unit-testable without a device or network.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.registration.fingerprint import snapshot_fingerprint
from src.registration.goal import build_registration_goal
from src.registration.output import append_account_row, row_from_parts
from src.registration.result import RegistrationResult
from src.registration.rotator import DeviceIdentityRotator, NoopRotator
from src.registration.tools import RegistrationSession, build_registration_tools
from src.worker.agent_runner.mobilerun_agent_runner import AgentFactoryRequest

logger = logging.getLogger(__name__)

_DEFAULT_CSV = "accounts.csv"
_DEFAULT_CONFIG = "config/mobilerun/config.yaml"
_DEFAULT_TIMEOUT = 1800
_DEFAULT_ARTIFACTS = "artifacts/registration"

SnapshotFn = Callable[..., Awaitable[Any]]
AgentFactory = Callable[[AgentFactoryRequest], Any]


class RegistrationRunner:
    """Orchestrate one registration attempt on a single device."""

    def __init__(
        self,
        *,
        device_serial: str,
        csv_path: str = _DEFAULT_CSV,
        country: str = "any",
        config_path: str = _DEFAULT_CONFIG,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        artifacts_dir: str = _DEFAULT_ARTIFACTS,
        agent_factory: AgentFactory | None = None,
        snapshot_fn: SnapshotFn | None = None,
        rotator: DeviceIdentityRotator | None = None,
        five_sim_client: Any | None = None,
        device_connection_type: str | None = None,
        device_genfarmer_id: str | None = None,
    ) -> None:
        if not device_serial:
            raise ValueError("device_serial is required")
        self._serial = device_serial
        self._csv_path = csv_path
        self._country = country
        self._config_path = config_path
        self._timeout = int(timeout_seconds)
        self._artifacts_dir = artifacts_dir
        self._agent_factory = agent_factory or _default_registration_factory
        self._snapshot_fn = snapshot_fn or snapshot_fingerprint
        self._rotator = rotator or NoopRotator()
        self._five_sim_client = five_sim_client
        # tailscale ip:port serials are TCP; default the label accordingly.
        self._conn_type = device_connection_type or (
            "tailscale" if ":" in device_serial else "usb"
        )
        self._genfarmer_id = device_genfarmer_id

    async def run(self) -> RegistrationResult:
        serial = self._serial
        run_artifacts = Path(self._artifacts_dir) / _safe_name(serial) / _timestamp_slug()
        run_artifacts.mkdir(parents=True, exist_ok=True)

        # 1. Rotate (capture-only locally) + verify reachable.
        rotation = await self._rotator.rotate(serial)
        if not rotation.ok:
            result = RegistrationResult(
                success=False,
                failure_reason="device_unreachable",
                notes=f"rotation: {rotation.detail}",
            )
            self._write_row(result, fingerprint={}, raw_getprop_path=None,
                            trajectory_path=str(run_artifacts), status="failed")
            return result

        # 2. Snapshot fingerprint (always).
        raw_getprop_path = run_artifacts / "getprop.txt"
        snap = await self._snapshot_fn(serial, raw_getprop_path=raw_getprop_path)
        fingerprint = dict(getattr(snap, "fields", {}) or {})
        raw_path = getattr(snap, "raw_getprop_path", None) or str(raw_getprop_path)

        # 3. Build the agent (goal + custom tools) and run it.
        session = RegistrationSession(
            client=self._five_sim_client,
            country=self._country,
            artifacts_dir=str(run_artifacts),
        )
        tools = build_registration_tools(session)
        goal = build_registration_goal(device_serial=serial, country=self._country)

        request = AgentFactoryRequest(
            goal=goal,
            device_serial=serial,
            variables={"device_serial": serial, "country": self._country},
            overrides={"use_tcp": ":" in serial, "trajectory_path": str(run_artifacts)},
            config_path=self._config_path,
            platform="instagram",
            timeout_seconds=self._timeout,
            tools=tuple(tools),
            output_model=_registration_output_model(),
        )

        try:
            agent = self._agent_factory(request)
            raw = await _await_agent_run(agent)
        except Exception as exc:
            logger.exception("registration agent run failed")
            result = RegistrationResult(
                success=False,
                failure_reason=f"agent_exception: {exc}",
                phone_number=session.phone_number,
                fivesim_order_id=session.order_id,
            )
            self._write_row(result, fingerprint, raw_path, str(run_artifacts), "failed")
            return result

        result = RegistrationResult.from_structured(
            getattr(raw, "structured_output", None)
        ) or RegistrationResult(success=False, failure_reason="no_structured_output")

        # Backfill phone/order from the session if the agent didn't echo them.
        if not result.phone_number and session.phone_number:
            result.phone_number = session.phone_number
        if not result.fivesim_order_id and session.order_id:
            result.fivesim_order_id = session.order_id
        if not result.phone_country:
            result.phone_country = self._country

        status = "success" if result.success else "failed"
        self._write_row(result, fingerprint, raw_path, str(run_artifacts), status)
        return result

    def _write_row(
        self,
        result: RegistrationResult,
        fingerprint: dict[str, Any],
        raw_getprop_path: str | None,
        trajectory_path: str,
        status: str,
    ) -> None:
        row = row_from_parts(
            result=result,
            fingerprint=fingerprint,
            device_adb_serial=self._serial,
            device_connection_type=self._conn_type,
            device_genfarmer_id=self._genfarmer_id,
            registered_at=_now_iso(),
            raw_getprop_path=raw_getprop_path,
            trajectory_path=trajectory_path,
            status=status,
        )
        append_account_row(self._csv_path, row)


# ---------------------------------------------------------------------------
# Default agent factory + output model (lazy mobilerun/pydantic)
# ---------------------------------------------------------------------------


def _default_registration_factory(request: AgentFactoryRequest) -> Any:
    """Build a real MobileRun agent for registration (lazy import)."""
    from src.worker.agent_runner.mobilerun_agent_runner import _default_agent_factory

    return _default_agent_factory(request)


def _registration_output_model() -> Any:
    """Lazy pydantic output model; ``None`` if pydantic is unavailable (tests)."""
    try:
        from src.registration.result import registration_result_pydantic_model

        return registration_result_pydantic_model()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _await_agent_run(agent: Any) -> Any:
    method = getattr(agent, "run", None)
    if method is None:
        raise RuntimeError("agent has no run() method")
    result = method()
    if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
        result = await result
    return result


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _safe_name(serial: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in serial)


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="registration")
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register", help="Register one Instagram account.")
    reg.add_argument("--device-serial", required=True, help="ADB serial (ip:port for TCP).")
    reg.add_argument("--country", default="any", help="5sim country (default: any).")
    reg.add_argument("--csv", default=_DEFAULT_CSV, help="Output CSV path.")
    reg.add_argument("--config", default=_DEFAULT_CONFIG, help="MobileRun config path.")
    reg.add_argument("--artifacts-dir", default=_DEFAULT_ARTIFACTS, help="Artifacts root.")
    reg.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT, help="Agent timeout (s).")
    reg.add_argument("--genfarmer-id", default=None, help="Optional GenFarmer device id.")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)

    if args.command == "register":
        runner = RegistrationRunner(
            device_serial=args.device_serial,
            csv_path=args.csv,
            country=args.country,
            config_path=args.config,
            timeout_seconds=args.timeout,
            artifacts_dir=args.artifacts_dir,
            device_genfarmer_id=args.genfarmer_id,
        )
        result = asyncio.run(runner.run())
        print(result.as_dict())
        return 0 if result.success else 1

    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
