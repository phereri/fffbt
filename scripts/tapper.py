#!/usr/bin/env python3
"""Tiny interactive tap helper for proving a UI sequence on ONE device.

Reads the live Portal a11y tree and taps a target by visible text, by
resource-id, or by explicit x,y. Always prints the resolved action. Used to
walk the Trial-Reel link-capture sequence step by step (with screenshots taken
separately) so a human can watch and confirm.

Usage:
    python scripts/tapper.py <serial> text "Trial reels"
    python scripts/tapper.py <serial> rid  share_button
    python scripts/tapper.py <serial> xy   540 1200
    python scripts/tapper.py <serial> dump            # print text nodes + bounds
"""
from __future__ import annotations

import asyncio
import selectors
import sys

sys.path.insert(0, ".")

from src.worker.agent_runner.custom_tools import build_instagram_custom_tools
from src.worker.tools._adb import shell as _shell
from src.worker.tools._ui import node_text, parse_bounds, walk_plain_ui


async def _read_ui(serial: str):
    from src.worker.agent_runner.custom_tools import _parse_portal_state
    raw = await _shell(serial, "content query --uri content://com.mobilerun.portal/state", timeout=15)
    return _parse_portal_state(raw)


async def _input_tap(serial: str, x: int, y: int) -> None:
    await _shell(serial, f"input tap {int(x)} {int(y)}", timeout=10)


async def main() -> int:
    serial, mode = sys.argv[1], sys.argv[2]
    tools = build_instagram_custom_tools(serial=serial)

    if mode == "dump":
        nodes = await _read_ui(serial)
        for n in nodes:
            t = node_text(n)
            b = parse_bounds(n.get("bounds"))
            rid = (n.get("resourceId") or n.get("resource_id") or "")
            if t or rid:
                print(f"  text={t!r:40} rid={str(rid).split('/')[-1]:30} bounds={b}")
        return 0

    if mode == "text":
        ok, msg = await tools["tap_by_text"]["function"](text=sys.argv[3])
        print(f"tap_by_text({sys.argv[3]!r}) -> ok={ok} {msg}")
        return 0 if ok else 1

    if mode == "rid":
        ok, msg = await tools["tap_by_resource_id"]["function"](resource_id=sys.argv[3])
        print(f"tap_by_resource_id({sys.argv[3]!r}) -> ok={ok} {msg}")
        return 0 if ok else 1

    if mode == "xy":
        x, y = int(sys.argv[3]), int(sys.argv[4])
        await _input_tap(serial, x, y)
        print(f"tap xy=({x},{y}) done")
        return 0

    print(f"unknown mode {mode!r}")
    return 2


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
