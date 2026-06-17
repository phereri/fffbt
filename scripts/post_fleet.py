#!/usr/bin/env python3
"""Single launcher that runs the proven Trial-Reel loop on several devices at once.

Each device gets one independent ``scripts/post_loop.py`` child process bound to
its own account (1 account = 1 device). The children claim videos from
``fffbt.videos`` with the existing atomic ``FOR UPDATE SKIP LOCKED`` claim, so
running them in parallel can never hand the same video to two devices — they
take rows in turn. No ``automation.*`` schema, no changeDevice, no app backups.

The account<->device binding comes from ``data/device_accounts.json`` (built by
``scripts/whoami.py``). Each child keeps its own per-account rate cap (20/24h),
cadence, recovery and escalation — this launcher only spawns and supervises.

Stopping: this process kills its children on exit; their PIDs are also written
to ``data/fleet_pids.json`` so they can be reaped manually if orphaned.

Usage:
  python scripts/post_fleet.py                 # all devices in the binding file
  python scripts/post_fleet.py 192.168.4.225:5555 192.168.5.41:5555   # subset
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.runner import fleet_events  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
PIDS_FILE = ROOT / "data" / "fleet_pids.json"
FLEET_LOG = os.environ.get("FLEET_LOG", str(ROOT / "post_fleet.log"))
VENV_PY = os.environ.get("LOOP_PY", str(ROOT / ".venv" / "Scripts" / "python.exe"))
POLL_SECS = int(os.environ.get("FLEET_POLL_SECS", "15"))

_children: list[subprocess.Popen] = []


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        with open(FLEET_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load_binding() -> dict[str, str]:
    data = json.loads(BINDING.read_text(encoding="utf-8"))
    devices = data.get("devices", {})
    if not isinstance(devices, dict) or not devices:
        raise SystemExit(f"no devices in {BINDING}")
    return devices


def _cleanup() -> None:
    for p in _children:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    # give them a moment, then hard-kill any stragglers
    deadline = time.time() + 10
    for p in _children:
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.3)
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
    try:
        PIDS_FILE.unlink()
    except Exception:
        pass


def _safe_account(account: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in account)


def main() -> int:
    devices = _load_binding()
    wanted = sys.argv[1:]
    if wanted:
        devices = {s: a for s, a in devices.items() if s in wanted}
        if not devices:
            raise SystemExit(f"none of {wanted} are in the binding file")

    atexit.register(_cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: sys.exit(0))
        except Exception:
            pass  # SIGTERM handler not settable on some platforms

    log(f"FLEET START devices={list(devices.keys())}")
    fleet_events.emit("fleet_start", devices=devices, pid=os.getpid())
    for serial, account in devices.items():
        per_log = ROOT / f"post_loop_{_safe_account(account)}.log"
        env = dict(
            os.environ,
            LOOP_DEVICE=serial,
            LOOP_ACCOUNT=account,
            LOOP_LOG=str(per_log),
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        p = subprocess.Popen(
            [VENV_PY, str(ROOT / "scripts" / "post_loop.py")],
            cwd=str(ROOT), env=env,
        )
        _children.append(p)
        log(f"FLEET spawned account={account} device={serial} pid={p.pid} log={per_log.name}")
        fleet_events.emit("fleet_spawned", account=account, device=serial,
                          pid=p.pid, log=per_log.name)

    PIDS_FILE.write_text(
        json.dumps({p.pid: dev for p, dev in zip(_children, devices.keys())}, indent=2),
        encoding="utf-8",
    )

    # Supervise: report each child's exit. We do NOT auto-restart — a clean
    # LOOP STOP is an escalation the operator asked to surface, and self-healing
    # already lives inside each loop.
    alive = {p.pid: devices_serial for p, devices_serial in zip(_children, devices.keys())}
    while any(p.poll() is None for p in _children):
        for p in _children:
            if p.pid in alive and p.poll() is not None:
                serial = alive[p.pid]
                log(f"FLEET child exited account-device={serial} pid={p.pid} rc={p.returncode}")
                fleet_events.emit("fleet_child_exit", account=devices.get(serial),
                                  device=serial, pid=p.pid, rc=p.returncode)
                del alive[p.pid]
        time.sleep(POLL_SECS)
    log("FLEET all children exited — STOP")
    fleet_events.emit("fleet_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
