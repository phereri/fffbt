"""ADB primitives scoped to a single device serial.

All functions take device_serial as the first argument. ADB binary path is
read from the ADB_PATH environment variable (default: ``adb``).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess


def _adb_path() -> str:
    return os.environ.get("ADB_PATH", "adb")


def _run_subprocess(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def shell(serial: str, cmd: str, timeout: int = 60) -> str:
    """Run ``adb -s <serial> shell <cmd>``."""
    args = [_adb_path(), "-s", serial, "shell", cmd]
    proc = await asyncio.to_thread(_run_subprocess, args, timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"adb shell rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    return proc.stdout or ""


async def push(serial: str, local: str, remote: str) -> None:
    """Run ``adb -s <serial> push <local> <remote>``."""
    args = [_adb_path(), "-s", serial, "push", local, remote]
    proc = await asyncio.to_thread(_run_subprocess, args, 600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"adb push rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )


async def input_tap(serial: str, x: int, y: int, *, hold_ms: int = 90) -> None:
    """Inject a touchscreen tap at (x, y) with display-override awareness.

    When the device has a ``wm size`` override, ``input touchscreen swipe``
    works in physical coordinates, so we query both sizes and scale.
    """
    px, py = x, y
    try:
        phys_out = (await shell(serial, "wm size", timeout=5)).strip()
        phys_match = override_match = None
        for ln in phys_out.splitlines():
            m = re.search(r"(\d+)x(\d+)", ln)
            if m:
                if "Override" in ln:
                    override_match = m
                elif "Physical" in ln:
                    phys_match = m
                elif phys_match is None:
                    phys_match = m
        if phys_match and override_match:
            pw, ph = int(phys_match.group(1)), int(phys_match.group(2))
            ow, oh = int(override_match.group(1)), int(override_match.group(2))
            if pw != ow or ph != oh:
                px = int(x * pw / ow)
                py = int(y * ph / oh)
    except Exception:
        pass

    cmd = f"input touchscreen swipe {px} {py} {px} {py} {max(40, int(hold_ms))}"
    args = [_adb_path(), "-s", serial, "shell", cmd]
    proc = await asyncio.to_thread(_run_subprocess, args, 30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"adb {cmd} rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:200]}"
        )


async def ime_input_shown(serial: str) -> bool:
    """True iff the device's current IME window is visible."""
    try:
        out = await shell(serial, "dumpsys input_method", timeout=10)
    except Exception:
        return False
    for line in out.splitlines():
        if "mInputShown=true" in line:
            return True
    return False


async def top_activity(serial: str) -> str:
    """Return the device's currently resumed Activity."""
    try:
        out = await shell(serial, "dumpsys activity activities", timeout=10)
    except Exception:
        return ""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("topResumedActivity=") or s.startswith("mResumedActivity="):
            for tok in s.split():
                if "/" in tok:
                    return tok
    return ""
