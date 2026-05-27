#!/usr/bin/env python3
"""Safe MobileWorker UI-tree preflight for one or more Android devices.

This diagnostic is intentionally non-posting: it does not tap, type, swipe,
create jobs, or mutate the database. It only connects the MobilerunWorker and
reads foreground activity plus UI-tree availability via preflight_ui_tree().
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.worker.session.mobilerun_adapter import MobilerunWorker


def _activity_name(activity: str | None) -> str | None:
    if not activity:
        return None
    for token in activity.split():
        if "/" in token:
            return token.strip()
    return activity.strip() or None


def _check_serial(serial: str, *, genfarmer_url: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "serial": serial,
        "ok": False,
        "ui_tree_available": False,
        "ui_tree_count": 0,
        "activityName": None,
        "error": None,
    }
    worker = MobilerunWorker(device_serial=serial, genfarmer_url=genfarmer_url)
    try:
        worker.connect()
        preflight = worker.preflight_ui_tree()
        ui_tree_available = bool(preflight.get("ui_tree_available"))
        result.update(
            {
                "ok": ui_tree_available,
                "ui_tree_available": ui_tree_available,
                "ui_tree_count": int(preflight.get("ui_tree_count") or 0),
                "activityName": _activity_name(preflight.get("activity")),
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        try:
            worker.disconnect()
        except Exception:
            pass
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run non-posting MobileWorker preflight for one or more devices."
    )
    parser.add_argument(
        "--serial",
        action="append",
        required=True,
        help="ADB device serial. Repeat for multiple devices.",
    )
    parser.add_argument(
        "--genfarmer-url",
        default=os.environ.get("GENFARMER_URL", "http://127.0.0.1:55554"),
        help="GenFarmer base URL. Defaults to GENFARMER_URL or localhost.",
    )
    args = parser.parse_args(argv)

    results = [
        _check_serial(serial, genfarmer_url=args.genfarmer_url)
        for serial in args.serial
    ]
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
