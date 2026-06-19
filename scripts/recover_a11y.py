#!/usr/bin/env python3
"""Fleet-wide accessibility recovery: toggle the Mobilerun a11y service + reboot,
then VERIFY the Portal actually returns an a11y tree on each device.

The publish agent reads the screen through the Mobilerun Portal a11y service. If
that service is enabled-but-not-bound (common after a reboot), the Portal returns
``Accessibility service not available`` and the agent dies mid-run. A clean
toggle+reboot rebinds it. This script does that across the binding (or a given
serial list), in parallel, and reports which devices have a working a11y tree
afterwards — only those are safe to launch.

Usage:
  python scripts/recover_a11y.py                 # all devices in the binding
  python scripts/recover_a11y.py 192.168.5.143:5555 192.168.5.141:5555
  python scripts/recover_a11y.py --verify-only    # skip reboot, just check a11y
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
ADB = os.environ.get("ADB_PATH", "adb")
IME = "com.mobilerun.portal/com.mobilerun.portal.service.MobilerunAccessibilityService"
STATE_URI = "content://com.mobilerun.portal/state"


def _adb(serial: str, *args, timeout=30):
    return subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True, timeout=timeout)


def _a11y_ok(serial: str) -> bool:
    """True if the Portal returns an a11y tree (service bound)."""
    try:
        out = _adb(serial, "shell", "content", "query", "--uri", STATE_URI, timeout=20).stdout
        return "a11y_tree" in out and '"status":"success"' in out.replace(" ", "")
    except Exception:
        return False


def _recover_one(serial: str, *, verify_only: bool) -> tuple[str, bool, str]:
    try:
        if not verify_only:
            _adb(serial, "shell", "settings", "delete", "secure", "enabled_accessibility_services", timeout=15)
            _adb(serial, "shell", "settings", "put", "secure", "accessibility_enabled", "0", timeout=15)
            time.sleep(1)
            _adb(serial, "shell", "settings", "put", "secure", "enabled_accessibility_services", IME, timeout=15)
            _adb(serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1", timeout=15)
            _adb(serial, "reboot", timeout=20)
            # WiFi adb devices do NOT auto-reconnect after a reboot — call
            # `adb connect <ip>` every 5s until the device reappears (operator
            # requirement), up to a 5 min timeout, then wait for boot_completed.
            booted = False
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(5)
                try:
                    subprocess.run([ADB, "connect", serial], capture_output=True, text=True, timeout=10)
                    if _adb(serial, "get-state", timeout=10).stdout.strip() == "device":
                        b = _adb(serial, "shell", "getprop", "sys.boot_completed", timeout=10).stdout.strip()
                        if b == "1":
                            booted = True
                            break
                except Exception:
                    pass
            if not booted:
                return serial, False, "did not reconnect/boot within 5 min"
            time.sleep(25)  # let Portal rebind a11y
        # verify (retry a few times — binding can lag)
        for _ in range(6):
            if _a11y_ok(serial):
                return serial, True, "a11y OK"
            time.sleep(5)
        return serial, False, "a11y still unavailable"
    except Exception as e:
        return serial, False, f"error: {e}"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    verify_only = "--verify-only" in sys.argv
    if args:
        serials = [s if ":" in s else f"{s}:5555" for s in args]
    else:
        serials = list(json.loads(BINDING.read_text(encoding="utf-8")).get("devices", {}).keys())
    print(f"{'verifying' if verify_only else 'recovering'} a11y on {len(serials)} devices…")
    with ThreadPoolExecutor(max_workers=min(len(serials), 24)) as ex:
        results = list(ex.map(lambda s: _recover_one(s, verify_only=verify_only), serials))
    good = [s for s, ok, _ in results if ok]
    bad = [(s, m) for s, ok, m in results if not ok]
    for s, ok, m in sorted(results):
        print(f"  {'OK  ' if ok else 'BAD '} {s}  {m}")
    print(f"\na11y GOOD: {len(good)}/{len(serials)}")
    if bad:
        print("NEEDS ATTENTION:", [s for s, _ in bad])
    # write the good list for the launcher
    (ROOT / "data" / "a11y_good.json").write_text(json.dumps(good, indent=2), encoding="utf-8")
    print(f"wrote good list -> data/a11y_good.json ({len(good)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
