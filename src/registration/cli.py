"""CLI + orchestration for one Instagram account registration.

    python -m src.registration.cli register --device-serial <S> \
        [--country any] [--csv accounts.csv]

Orchestration per run (``RegistrationRunner.run``):
  1. ``rotate(serial)`` via the configured ``DeviceIdentityRotator`` (NoopRotator
     for test = skip ChangeDevice) and verify the device is reachable.
  2. Get a fresh, account-free Instagram onto the device. Fast path: restore a
     golden clean-data backup (``--clean-backup``) via the genfarmer root shell —
     no slow APK download. Slow path: install the APK fresh (``--apk``). The
     clean backup needs Instagram already installed, so a one-time APK bootstrap
     is used if the package is missing and an APK was supplied.
  3. Snapshot the device fingerprint + dump raw getprop to artifacts.
  4. Build a MobileRun agent with the registration goal + the custom tools
     (buy_phone_number / get_sms_code / ask_operator) and run it.
  5. On success: save the bundle (fingerprint profile + credentials + Instagram
     app backup via genfarmer root shell) so next session can be restored
     without re-login.

Everything external (agent factory, fingerprint snapshot, rotator, 5sim client,
app backup client) is injectable so the orchestration is unit-testable without
a device or network.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.genfarmer.app_backup import AppBackupClient, BackupResult, default_backup_client
from src.registration.fingerprint import snapshot_fingerprint
from src.registration.goal import build_registration_goal
from src.registration.output import append_account_row, row_from_parts
from src.registration.result import RegistrationResult
from src.registration.rotator import DeviceIdentityRotator, NoopRotator
from src.registration.tools import RegistrationSession, build_custom_tools
from src.worker.agent_runner.mobilerun_agent_runner import AgentFactoryRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APK install helper
# ---------------------------------------------------------------------------

INSTAGRAM_PACKAGE = "com.instagram.android"


def _install_fresh_apk(
    serial: str,
    apk_path: str | Path,
    *,
    adb_bin: str | None = None,
    timeout: float = 120.0,
) -> None:
    """Ensure a fresh Instagram install (no account data) on the device.

    Steps:
      1. Uninstall existing Instagram (ignore if not installed).
      2. Install the APK from the given path.
      3. Verify it's installed.
    """
    adb = adb_bin or os.environ.get("ADB_BIN") or os.environ.get("ADB") or "adb"
    apk = Path(apk_path)
    if not apk.is_file():
        raise FileNotFoundError(f"APK not found: {apk}")

    def _run(*args: str) -> subprocess.CompletedProcess:
        cmd = [adb, "-s", serial, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # Uninstall existing (ignore errors — may not be installed)
    logger.info("Uninstalling %s from %s...", INSTAGRAM_PACKAGE, serial)
    r = _run("uninstall", INSTAGRAM_PACKAGE)
    if r.returncode == 0:
        logger.info("  uninstalled OK")
    else:
        logger.info("  not installed or uninstall skipped: %s", (r.stdout or r.stderr or "").strip())

    # Install fresh APK
    logger.info("Installing APK: %s", apk.name)
    r = _run("install", "-r", str(apk))
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"APK install failed: {err}")
    logger.info("  install OK: %s", (r.stdout or "").strip()[:80])

    # Verify
    r = _run("shell", "pm", "path", INSTAGRAM_PACKAGE)
    if "package:" not in (r.stdout or ""):
        raise RuntimeError(f"Verification failed: {INSTAGRAM_PACKAGE} not found after install")
    logger.info("  verified: %s installed", INSTAGRAM_PACKAGE)


def _file_operator_input(
    artifacts_dir: str | Path, *, poll_seconds: float = 2.0, timeout_seconds: float = 1800.0
) -> Callable[[str], str]:
    """Operator I/O over files, so ``ask_operator`` works on a detached run.

    ``input()`` raises EOF when the process has no attached TTY (e.g. launched
    over ssh in the background). Instead, write the agent's question to
    ``operator_request.txt`` in the run's artifacts dir and block until an
    operator drops ``operator_answer.txt`` beside it (poll). On timeout, return a
    sentinel telling the agent to use its best judgment.
    """

    adir = Path(artifacts_dir)
    req = adir / "operator_request.txt"
    ans = adir / "operator_answer.txt"

    def _ask(prompt: str) -> str:
        adir.mkdir(parents=True, exist_ok=True)
        if ans.exists():  # clear any stale answer from a previous question
            ans.unlink()
        req.write_text(prompt, encoding="utf-8")
        # Mirror to stdout/log too, so a human tailing the run sees the prompt.
        print(prompt, flush=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if ans.exists():
                answer = ans.read_text(encoding="utf-8").strip()
                ans.unlink()
                req.unlink(missing_ok=True)
                return answer
            time.sleep(poll_seconds)
        req.unlink(missing_ok=True)
        return (
            "TIMEOUT: no operator answered within the wait window. Use your best "
            "judgment; if you cannot safely proceed, set success=false with a "
            "failure_reason describing exactly where you are stuck."
        )

    return _ask


def _adb_capture(serial: str, artifacts_dir: str | Path) -> CaptureFn:
    """Capture a screenshot + UI-tree dump on each ``ask_operator`` call.

    Lets a human operator (or another agent) actually see the stuck screen. Best
    effort: any adb failure is reported in the returned dict, never raised.
    """

    adb = os.environ.get("ADB_BIN") or os.environ.get("ADB_PATH") or "adb"
    adir = Path(artifacts_dir)
    counter = {"n": 0}

    async def _capture(question: str) -> dict[str, Any]:
        adir.mkdir(parents=True, exist_ok=True)
        counter["n"] += 1
        n = counter["n"]
        out: dict[str, Any] = {}

        def _run(args: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run([adb, "-s", serial, *args], capture_output=True, timeout=30)

        try:
            r = await asyncio.to_thread(_run, ["exec-out", "screencap", "-p"])
            if r.returncode == 0 and r.stdout:
                png = adir / f"ask_{n}_screen.png"
                png.write_bytes(r.stdout)
                out["screenshot"] = str(png)
            else:
                out["screenshot_error"] = (r.stderr or b"").decode(errors="replace")[:120]
        except Exception as exc:  # noqa: BLE001
            out["screenshot_error"] = str(exc)

        try:
            await asyncio.to_thread(_run, ["shell", "uiautomator", "dump", "/sdcard/wd.xml"])
            r2 = await asyncio.to_thread(_run, ["shell", "cat", "/sdcard/wd.xml"])
            if r2.returncode == 0 and r2.stdout:
                xml = adir / f"ask_{n}_ui.xml"
                xml.write_bytes(r2.stdout)
                out["ui"] = str(xml)
        except Exception as exc:  # noqa: BLE001
            out["ui_error"] = str(exc)
        return out

    return _capture


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
        apk_path: str | Path | None = None,
        clean_backup_dir: str | Path | None = None,
        csv_path: str = _DEFAULT_CSV,
        country: str = "any",
        operator: str = "any",
        max_price: float | None = None,
        sms_timeout_seconds: float = 300.0,
        sms_poll_window_seconds: float = 30.0,
        config_path: str = _DEFAULT_CONFIG,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
        artifacts_dir: str = _DEFAULT_ARTIFACTS,
        agent_factory: AgentFactory | None = None,
        snapshot_fn: SnapshotFn | None = None,
        rotator: DeviceIdentityRotator | None = None,
        five_sim_client: Any | None = None,
        backup_client: AppBackupClient | None = None,
        device_connection_type: str | None = None,
        device_genfarmer_id: str | None = None,
    ) -> None:
        if not device_serial:
            raise ValueError("device_serial is required")
        self._serial = device_serial
        self._apk_path = Path(apk_path) if apk_path else None
        self._clean_backup_dir = Path(clean_backup_dir) if clean_backup_dir else None
        self._csv_path = csv_path
        self._country = country
        self._operator = operator
        self._max_price = max_price
        self._sms_timeout = float(sms_timeout_seconds)
        self._sms_poll_window = float(sms_poll_window_seconds)
        self._config_path = config_path
        self._timeout = int(timeout_seconds)
        self._artifacts_dir = artifacts_dir
        self._agent_factory = agent_factory or _default_registration_factory
        self._snapshot_fn = snapshot_fn or snapshot_fingerprint
        self._rotator = rotator or NoopRotator()
        self._five_sim_client = five_sim_client
        self._backup_client = backup_client
        # tailscale ip:port serials are TCP; default the label accordingly.
        self._conn_type = device_connection_type or (
            "tailscale" if ":" in device_serial else "usb"
        )
        self._genfarmer_id = device_genfarmer_id

    async def run(self) -> RegistrationResult:
        serial = self._serial
        run_artifacts = Path(self._artifacts_dir) / _safe_name(serial) / _timestamp_slug()
        run_artifacts.mkdir(parents=True, exist_ok=True)

        # 1. Rotate (capture-only locally / NoopRotator for test) + verify reachable.
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

        # 2. Get a fresh, account-free Instagram onto the device (restore clean
        #    backup / install APK — see _prepare_instagram).
        prep_error = self._prepare_instagram(serial)
        if prep_error:
            logger.error("Instagram prep failed: %s", prep_error)
            result = RegistrationResult(success=False, failure_reason=prep_error)
            self._write_row(result, fingerprint={}, raw_getprop_path=None,
                            trajectory_path=str(run_artifacts), status="failed")
            return result

        # 3. Snapshot fingerprint (always).
        raw_getprop_path = run_artifacts / "getprop.txt"
        snap = await self._snapshot_fn(serial, raw_getprop_path=raw_getprop_path)
        fingerprint = dict(getattr(snap, "fields", {}) or {})
        raw_path = getattr(snap, "raw_getprop_path", None) or str(raw_getprop_path)

        # 4. Build the agent (goal + custom tools) and run it.
        session = RegistrationSession(
            client=self._five_sim_client,
            country=self._country,
            operator=self._operator,
            max_price=self._max_price,
            code_timeout=self._sms_timeout,
            code_poll_window=self._sms_poll_window,
            artifacts_dir=str(run_artifacts),
            operator_input=_file_operator_input(run_artifacts),
            capture_fn=_adb_capture(serial, run_artifacts),
        )
        tools = build_custom_tools(session)
        goal = build_registration_goal(device_serial=serial, country=self._country)

        request = AgentFactoryRequest(
            goal=goal,
            device_serial=serial,
            variables={"device_serial": serial, "country": self._country},
            overrides={"use_tcp": ":" in serial, "trajectory_path": str(run_artifacts),
                       "max_steps": 70},
            config_path=self._config_path,
            platform="instagram",
            timeout_seconds=self._timeout,
            tools=tools,
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

        # 5. On success: save bundle (fingerprint + credentials + app backup).
        backup_result: BackupResult | None = None
        if result.success and self._backup_client:
            logger.info("Registration succeeded — backing up Instagram app data...")
            label = result.username or _timestamp_slug()
            backup_result = self._backup_client.backup(
                serial, INSTAGRAM_PACKAGE, label=label,
            )
            if backup_result.ok:
                logger.info(
                    "App backup saved: %s (%d bytes)",
                    backup_result.backup_dir, backup_result.archive_size_bytes,
                )
                # Save fingerprint profile into the same backup dir
                if backup_result.backup_dir:
                    fp_path = backup_result.backup_dir / "fingerprint.json"
                    fp_path.write_text(
                        json.dumps(fingerprint, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    creds_path = backup_result.backup_dir / "credentials.json"
                    creds_path.write_text(
                        json.dumps(result.as_dict(), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
            else:
                logger.warning("App backup FAILED: %s", backup_result.error)

        status = "success" if result.success else "failed"
        self._write_row(result, fingerprint, raw_path, str(run_artifacts), status)
        return result

    def _prepare_instagram(self, serial: str) -> str | None:
        """Ensure a fresh, account-free Instagram is on the device.

        Fast path: restore the golden clean-data backup (``--clean-backup``) via
        the genfarmer root shell — no slow APK download. If Instagram isn't
        installed yet and an APK is supplied, install it once as a bootstrap,
        then restore. Slow path: install the APK fresh. With neither configured
        (e.g. unit tests, or the app is already in a clean state) this is a no-op.

        Returns ``None`` on success or a ``failure_reason`` string on error.
        """
        if self._clean_backup_dir:
            if self._backup_client is None:
                return "clean_backup_requested_but_no_backup_client"
            res = self._backup_client.restore(serial, INSTAGRAM_PACKAGE, self._clean_backup_dir)
            if res.ok:
                logger.info("Restored clean Instagram backup from %s", self._clean_backup_dir)
                return None
            # Package not installed yet → one-time APK bootstrap, then restore.
            if "not installed" in res.error.lower() and self._apk_path:
                logger.info("Instagram absent — bootstrapping APK once, then restoring clean data...")
                try:
                    _install_fresh_apk(serial, self._apk_path)
                except (FileNotFoundError, RuntimeError) as exc:
                    return f"apk_install_failed: {exc}"
                res = self._backup_client.restore(serial, INSTAGRAM_PACKAGE, self._clean_backup_dir)
                if res.ok:
                    logger.info("Restored clean Instagram backup after APK bootstrap")
                    return None
            return f"clean_restore_failed: {res.error}"

        if self._apk_path:
            try:
                _install_fresh_apk(serial, self._apk_path)
            except (FileNotFoundError, RuntimeError) as exc:
                return f"apk_install_failed: {exc}"
        return None

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
    reg.add_argument("--apk", default=None,
                     help="Path to Instagram APK. If given, uninstall+install fresh before registration.")
    reg.add_argument("--clean-backup", default=None,
                     help="Path to a golden clean-Instagram backup dir (data.tgz + manifest.json). "
                          "Restored before registration instead of installing the APK (fast path). "
                          "Falls back to a one-time --apk bootstrap if Instagram isn't installed.")
    reg.add_argument("--provider", default="5sim", choices=["5sim", "smspool"],
                     help="SMS number provider (default: 5sim). smspool = non-VoIP real-SIM.")
    reg.add_argument("--country", default="any", help="SMS country (e.g. vietnam; default: any).")
    reg.add_argument("--operator", default="any", help="5sim operator priority list, e.g. 'virtual4,any' (default: any).")
    reg.add_argument("--max-price", type=float, default=None,
                     help="5sim per-number price cap (raises it; unlocks pricier high-delivery operators).")
    reg.add_argument("--sms-timeout", type=float, default=300.0,
                     help="Total seconds to wait for the SMS code per number, across resend retries (default: 300).")
    reg.add_argument("--sms-poll-window", type=float, default=30.0,
                     help="Seconds get_sms_code polls per call before telling the agent to resend (default: 30).")
    reg.add_argument("--csv", default=_DEFAULT_CSV, help="Output CSV path.")
    reg.add_argument("--config", default=_DEFAULT_CONFIG, help="MobileRun config path.")
    reg.add_argument("--artifacts-dir", default=_DEFAULT_ARTIFACTS, help="Artifacts root.")
    reg.add_argument("--backup-root", default=None,
                     help="Root dir for app backups (default: app_backups/).")
    reg.add_argument("--no-backup", action="store_true",
                     help="Skip app backup on success.")
    reg.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT, help="Agent timeout (s).")
    reg.add_argument("--genfarmer-id", default=None, help="Optional GenFarmer device id.")

    # -- register-loop: self-recovering SMS recipe ladder -------------------
    loop = sub.add_parser(
        "register-loop",
        help="Register with self-recovery: try a ladder of SMS recipes "
             "(provider/country/operator) until one delivers, restoring the "
             "clean backup between attempts.",
    )
    loop.add_argument("--device-serial", required=True, help="ADB serial (ip:port for TCP).")
    loop.add_argument("--apk", default=None, help="Instagram APK (one-time bootstrap if absent).")
    loop.add_argument("--clean-backup", default=None,
                      help="Golden clean-Instagram backup dir, restored before EACH attempt.")
    loop.add_argument("--recipes", default=None,
                      help="Path to a JSON ladder (list of recipe objects). Default: built-in ladder.")
    loop.add_argument("--max-attempts", type=int, default=None,
                      help="Cap the number of recipes tried (default: all).")
    loop.add_argument("--csv", default=_DEFAULT_CSV, help="Output CSV path.")
    loop.add_argument("--config", default=_DEFAULT_CONFIG, help="MobileRun config path.")
    loop.add_argument("--artifacts-dir", default=_DEFAULT_ARTIFACTS, help="Artifacts root.")
    loop.add_argument("--backup-root", default=None, help="Root dir for app backups.")
    loop.add_argument("--no-backup", action="store_true", help="Skip app backup on success.")
    loop.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT, help="Agent timeout (s).")
    loop.add_argument("--genfarmer-id", default=None, help="Optional GenFarmer device id.")
    return parser


def _make_sms_client(provider: str) -> Any:
    """Pluggable SMS-number provider. Add new providers here behind the same
    interface (buy_number / get_code / cancel / ban / finish / balance)."""
    if provider == "smspool":
        from src.registration.sms_pool import SmsPoolClient

        return SmsPoolClient()
    from src.registration.five_sim import FiveSimClient

    return FiveSimClient()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)

    if args.command == "register":
        # Build backup client (unless --no-backup)
        backup_client: AppBackupClient | None = None
        if not args.no_backup:
            backup_root = Path(args.backup_root) if args.backup_root else None
            backup_client = default_backup_client(backup_root=backup_root)

        runner = RegistrationRunner(
            device_serial=args.device_serial,
            apk_path=args.apk,
            clean_backup_dir=args.clean_backup,
            csv_path=args.csv,
            country=args.country,
            five_sim_client=_make_sms_client(args.provider),
            operator=args.operator,
            max_price=args.max_price,
            sms_timeout_seconds=args.sms_timeout,
            sms_poll_window_seconds=args.sms_poll_window,
            config_path=args.config,
            timeout_seconds=args.timeout,
            artifacts_dir=args.artifacts_dir,
            backup_client=backup_client,
            device_genfarmer_id=args.genfarmer_id,
        )
        result = asyncio.run(runner.run())
        print(result.as_dict())
        return 0 if result.success else 1

    if args.command == "register-loop":
        from src.registration.ladder import (
            DEFAULT_LADDER,
            DeviceConfig,
            RecipeLadder,
            default_runner_factory,
            load_recipes,
        )

        backup_client = None
        if not args.no_backup:
            backup_root = Path(args.backup_root) if args.backup_root else None
            backup_client = default_backup_client(backup_root=backup_root)

        recipes = list(load_recipes(args.recipes)) if args.recipes else list(DEFAULT_LADDER)
        cfg = DeviceConfig(
            device_serial=args.device_serial,
            clean_backup_dir=args.clean_backup,
            apk_path=args.apk,
            csv_path=args.csv,
            config_path=args.config,
            artifacts_dir=args.artifacts_dir,
            backup_client=backup_client,
            genfarmer_id=args.genfarmer_id,
            timeout_seconds=args.timeout,
            # NoopRotator: on the Tailscale test setup the heavy ChangeDevice is
            # driven once up front by the operator (it reboots + drops Tailscale).
            rotator=NoopRotator(),
        )
        ladder = RecipeLadder(
            recipes,
            default_runner_factory(cfg),
            max_attempts=args.max_attempts,
        )
        outcome = asyncio.run(ladder.run())
        print("LADDER:", outcome.stopped_reason)
        for a in outcome.attempts:
            print("  ", a.summary())
        if outcome.final:
            print(outcome.final.as_dict())
        return 0 if outcome.success else 1

    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
