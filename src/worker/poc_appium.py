"""Appium PoC — verify UiAutomator2 connectivity to one Android device.

Runs through the FFF-27 acceptance criteria:
  1. Appium server reachable
  2. UiAutomator2 session created
  3. Screenshot captured
  4. Page source (UI XML) retrieved
  5. Tap executed on a safe target

Usage:
    python -m src.worker.poc_appium --device-serial <serial> \
        [--appium-url http://127.0.0.1:4723] \
        [--artifacts-dir ./.artifacts]

Requires: pip install Appium-Python-Client
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _log(check: str, passed: bool, detail: str = "") -> dict:
    status = "PASS" if passed else "FAIL"
    msg = f"[{status}] {check}"
    if detail:
        msg += f" — {detail}"
    print(msg, file=sys.stderr)
    return {"check": check, "passed": passed, "detail": detail}


def _check_appium_reachable(appium_url: str) -> tuple[bool, str]:
    resp = urlopen(f"{appium_url}/status", timeout=10)
    data = json.loads(resp.read())
    build = data.get("value", {}).get("build", {})
    version = build.get("version", "unknown")
    return True, f"version={version}"


def _check_session(appium_url: str, device_serial: str):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options

    opts = UiAutomator2Options()
    opts.platform_name = "Android"
    opts.automation_name = "UiAutomator2"
    opts.udid = device_serial
    opts.no_reset = True
    opts.new_command_timeout = 120

    driver = webdriver.Remote(appium_url, options=opts)
    return driver


def _check_screenshot(driver, out_dir: Path) -> tuple[bool, str]:
    path = str(out_dir / "screenshot.png")
    driver.save_screenshot(path)
    size = os.path.getsize(path)
    return True, f"saved={path} bytes={size}"


def _check_page_source(driver, out_dir: Path) -> tuple[bool, str]:
    source = driver.page_source
    path = out_dir / "page_source.xml"
    path.write_text(source, encoding="utf-8")
    return True, f"saved={path} length={len(source)}"


def _check_tap(driver, out_dir: Path) -> tuple[bool, str]:
    driver.press_keycode(3)
    time.sleep(1)

    elements = driver.find_elements("xpath", "//*[@clickable='true']")
    if not elements:
        return False, "no clickable elements found on home screen"

    target = elements[0]
    text = target.get_attribute("text") or ""
    desc = target.get_attribute("content-desc") or ""
    label = text or desc or target.tag_name
    target.click()
    time.sleep(0.5)

    driver.save_screenshot(str(out_dir / "post_tap_screenshot.png"))
    driver.press_keycode(3)
    return True, f"tapped: {label!r}"


def run_poc(
    appium_url: str,
    device_serial: str,
    artifacts_dir: str,
) -> list[dict]:
    results: list[dict] = []
    out = Path(artifacts_dir) / "poc_appium" / _ts()
    out.mkdir(parents=True, exist_ok=True)

    try:
        ok, detail = _check_appium_reachable(appium_url)
        results.append(_log("appium_reachable", ok, detail))
    except Exception as exc:
        results.append(_log("appium_reachable", False, str(exc)))
        return results

    driver = None
    try:
        driver = _check_session(appium_url, device_serial)
        results.append(_log("session_created", True, f"session={driver.session_id}"))
    except ImportError:
        results.append(_log("session_created", False, "Appium-Python-Client not installed (pip install Appium-Python-Client)"))
        return results
    except Exception as exc:
        results.append(_log("session_created", False, str(exc)))
        return results

    try:
        for name, fn in [
            ("screenshot", lambda: _check_screenshot(driver, out)),
            ("page_source", lambda: _check_page_source(driver, out)),
            ("tap_safe_target", lambda: _check_tap(driver, out)),
        ]:
            try:
                ok, detail = fn()
                results.append(_log(name, ok, detail))
            except Exception as exc:
                results.append(_log(name, False, str(exc)))
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Appium PoC — FFF-27 acceptance criteria")
    parser.add_argument("--device-serial", required=True, help="ADB serial of the target device")
    parser.add_argument(
        "--appium-url",
        default=os.environ.get("APPIUM_BASE_URL", "http://127.0.0.1:4723"),
    )
    parser.add_argument(
        "--artifacts-dir",
        default=os.environ.get("ARTIFACTS_DIR", "./.artifacts"),
    )
    args = parser.parse_args()

    results = run_poc(args.appium_url, args.device_serial, args.artifacts_dir)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"\n{'='*40}", file=sys.stderr)
    print(f"Appium PoC: {passed}/{total} checks passed", file=sys.stderr)

    print(json.dumps({"checks": results, "passed": passed, "total": total}, indent=2))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
