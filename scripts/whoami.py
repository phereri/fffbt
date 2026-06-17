#!/usr/bin/env python3
"""Read the logged-in Instagram username from a device (read-only).

Opens IG cleanly, taps the Profile tab, and reads the active username from the
profile action-bar. No posting, no writes. Used once to bind account<->IP for
the fleet launcher.

Usage:
  python scripts/whoami.py <serial>            # print resolved username
  python scripts/whoami.py <serial> --dump     # dump top-bar candidate nodes
"""
from __future__ import annotations

import asyncio
import selectors
import sys

sys.path.insert(0, ".")

from src.worker.agent_runner.custom_tools import _parse_portal_state
from src.worker.tools._adb import shell
from src.worker.tools._ui import node_resource_id, node_text, parse_bounds

SETTLE = 1.4
IG_PKG = "com.instagram.android"


async def read_ui(serial: str):
    raw = await shell(serial, "content query --uri content://com.mobilerun.portal/state", timeout=15)
    return _parse_portal_state(raw)


def _center(n):
    if not n:
        return None
    b = parse_bounds(n.get("bounds"))
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) if b else None


def _by_rid(nodes, suffix):
    for n in nodes:
        if node_resource_id(n).endswith(suffix) and parse_bounds(n.get("bounds")):
            return n
    return None


def _by_text(nodes, needle, *, min_y=None):
    nl = needle.lower()
    best = None
    for n in nodes:
        if nl not in node_text(n).lower():
            continue
        b = parse_bounds(n.get("bounds"))
        if not b:
            continue
        cy = (b[1] + b[3]) // 2
        if min_y is not None and cy < min_y:
            continue
        best = n
    return best


async def tap(serial, xy, label=""):
    if not xy:
        return False
    await shell(serial, f"input tap {int(xy[0])} {int(xy[1])}", timeout=10)
    await asyncio.sleep(SETTLE)
    return True


async def _open_profile(serial: str):
    # clean launch
    await shell(serial, f"am force-stop {IG_PKG}", timeout=10)
    await asyncio.sleep(1.0)
    await shell(serial, f"monkey -p {IG_PKG} -c android.intent.category.LAUNCHER 1", timeout=15)
    await asyncio.sleep(5.0)
    # tap Profile tab (bottom-right); retry a couple of times
    for _ in range(4):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "action_bar_large_title_auto_size") or _by_rid(nodes, "profile_header_full_name"):
            return nodes
        prof = _by_text(nodes, "Profile", min_y=1650)
        if prof is not None:
            await tap(serial, _center(prof), "Profile tab")
            continue
        # fallback: tap the rightmost bottom-nav slot
        await asyncio.sleep(SETTLE)
    return await read_ui(serial)


# resource-id suffixes that, on the own-profile screen, carry the @username
_USERNAME_RIDS = (
    "action_bar_large_title_auto_size",
    "action_bar_title",
    "action_bar_textview_title",
    "title_view",
)


def resolve_username(nodes):
    for rid in _USERNAME_RIDS:
        n = _by_rid(nodes, rid)
        txt = node_text(n).strip() if n else ""
        # username: no spaces, plausible handle chars
        if txt and " " not in txt and all(c.isalnum() or c in "._" for c in txt):
            return txt, rid
    return None, None


async def main():
    serial = sys.argv[1]
    dump = "--dump" in sys.argv[2:]
    nodes = await _open_profile(serial)
    if dump:
        print("== top-bar nodes (y<340) ==")
        for n in nodes:
            b = parse_bounds(n.get("bounds"))
            if not b or b[1] > 340:
                continue
            rid = node_resource_id(n)
            txt = node_text(n)
            if rid or txt:
                print(f"  y={b[1]:>4} rid={rid!r} text={txt!r}")
        print("== candidates by known rids ==")
        for rid in _USERNAME_RIDS:
            n = _by_rid(nodes, rid)
            print(f"  {rid}: {node_text(n)!r}" if n else f"  {rid}: <absent>")
    user, rid = resolve_username(nodes)
    print(f"\nUSERNAME: {user}   (via {rid})")
    return 0 if user else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
