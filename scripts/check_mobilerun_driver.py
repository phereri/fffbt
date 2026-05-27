#!/usr/bin/env python3
"""Read a Mobilerun AndroidDriver UI tree from one connected device.

This is a safe VPS diagnostic: it does not tap, type, post, reset, or mutate
device state. It only connects to the Mobilerun Portal through the Python
``mobilerun`` package and saves the returned tree for inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any


def _timestamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def _tree_len(payload: Any) -> int:
    if isinstance(payload, dict):
        tree = payload.get("a11y_tree")
        if isinstance(tree, list):
            return len(tree)
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return 0
        if isinstance(inner, dict):
            tree = inner.get("a11y_tree")
            if isinstance(tree, list):
                return len(tree)
    return 0


def _phone_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    state = payload.get("phone_state")
    if isinstance(state, dict):
        return state
    inner = payload.get("result")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            return {}
    if isinstance(inner, dict) and isinstance(inner.get("phone_state"), dict):
        return inner["phone_state"]
    return {}


async def _read_tree(serial: str, use_tcp: bool) -> Any:
    try:
        from mobilerun import AndroidDriver
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "mobilerun is not installed. Run: pip install -r src/worker/requirements.txt"
        ) from exc

    driver = AndroidDriver(serial=serial, use_tcp=use_tcp)
    await driver.connect()
    return await driver.get_ui_tree()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read Mobilerun AndroidDriver.get_ui_tree() for one device."
    )
    parser.add_argument("--serial", required=True, help="ADB device serial, e.g. 100.110.232.89:5555")
    parser.add_argument(
        "--use-tcp",
        action="store_true",
        help="Pass use_tcp=True to AndroidDriver. Default is False, matching the imported farm code.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=os.environ.get("ARTIFACTS_DIR", "./.artifacts"),
        help="Directory for JSON output. Defaults to ARTIFACTS_DIR or ./.artifacts.",
    )
    args = parser.parse_args(argv)

    payload = asyncio.run(_read_tree(args.serial, args.use_tcp))

    out_dir = pathlib.Path(args.artifacts_dir) / "mobilerun_driver"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_timestamp()}_{args.serial.replace(':', '_')}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    state = _phone_state(payload)
    summary = {
        "serial": args.serial,
        "use_tcp": args.use_tcp,
        "a11y_tree_count": _tree_len(payload),
        "activityName": state.get("activityName"),
        "keyboardVisible": state.get("keyboardVisible"),
        "isEditable": state.get("isEditable"),
        "artifact": str(out_path),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
