#!/usr/bin/env python3
"""Deterministic Trial-Reel link capture (proof harness).

Navigates: Profile -> Reels tab -> first "Drafts and trial reels" tile ->
'Trial reels' -> first (newest) tile -> Share -> 'Copy link', then pastes the
clipboard into the share-sheet search box and reads the real URL from the a11y
tree. No LLM agent — reads an actual field value, so it cannot hallucinate.

Usage: python scripts/capture_link.py <serial>
"""
from __future__ import annotations

import asyncio
import selectors
import sys

sys.path.insert(0, ".")

from src.worker.agent_runner.custom_tools import _parse_portal_state
from src.worker.tools._adb import shell
from src.worker.tools._ui import node_resource_id, node_text, parse_bounds

SETTLE = 1.2


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


def _by_text(nodes, needle, *, smallest=True, max_y=None, min_y=None):
    nl = needle.lower()
    cands = []
    for n in nodes:
        if nl not in node_text(n).lower():
            continue
        b = parse_bounds(n.get("bounds"))
        if not b:
            continue
        cy = (b[1] + b[3]) // 2
        if max_y is not None and cy > max_y:
            continue
        if min_y is not None and cy < min_y:
            continue
        cands.append(n)
    if not cands:
        return None
    area = lambda n: (lambda b: (b[2] - b[0]) * (b[3] - b[1]))(parse_bounds(n["bounds"]))
    return min(cands, key=area) if smallest else max(cands, key=area)


def clean_reel_url(text):
    if not text:
        return None
    marker = "instagram.com/reel/"
    i = text.find(marker)
    if i == -1:
        return None
    start = text.rfind("http", 0, i)
    url = text[start if start != -1 else i:].strip()
    for ws in (" ", "\n", "\t", '"', "'"):
        j = url.find(ws)
        if j != -1:
            url = url[:j]
    url = url.split("?")[0].split("#")[0]
    if not url.endswith("/"):
        url += "/"
    return url if "instagram.com/reel/" in url else None


async def tap(serial, xy, label=""):
    if not xy:
        print(f"  [skip] {label}: no target")
        return False
    # Raw `input tap` — reliable on this device (the input_tap helper's scaled
    # touchscreen-swipe misfires here).
    await shell(serial, f"input tap {int(xy[0])} {int(xy[1])}", timeout=10)
    print(f"  tapped {label} at {xy}")
    await asyncio.sleep(SETTLE)
    return True


async def capture(serial: str):
    # 0) ensure profile Reels grid: tap Profile tab, then Reels sub-tab
    def _find_reels_tab(nodes):
        for n in nodes:
            if node_resource_id(n).endswith("profile_tab_icon_view") and node_text(n).strip().lower() == "reels":
                return n
        return None

    for attempt in range(4):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "drafts_text"):
            break  # already on the Reels grid
        reels_tab = _find_reels_tab(nodes)
        if reels_tab is not None:
            await tap(serial, _center(reels_tab), "Reels sub-tab")
            continue
        prof = _by_text(nodes, "Profile", min_y=1650)
        if prof is not None:
            await tap(serial, _center(prof), "Profile tab")
            continue
        await asyncio.sleep(SETTLE)

    # 1) open Drafts/Trial-reels selector: tap the thumbnail of the first tile
    nodes = await read_ui(serial)
    dt = _by_rid(nodes, "drafts_text")
    if not dt:
        print("  [fail] drafts_text tile not found (not on Reels grid?)")
        return None
    b = parse_bounds(dt["bounds"])
    await tap(serial, ((b[0] + b[2]) // 2, b[1] - (b[3] - b[1])), "Drafts/Trial tile thumbnail")

    # 2) tap 'Trial reels' row (retry; the row can ignore the first tap)
    for _ in range(3):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "trials_list") or _by_text(nodes, "Create trial reel"):
            break
        row = _by_text(nodes, "Trial reels")
        await tap(serial, _center(row), "'Trial reels' row")

    # 3) open first (newest) tile in trials_list
    nodes = await read_ui(serial)
    tl = _by_rid(nodes, "trials_list")
    if not tl:
        print("  [fail] trials_list not reached")
        return None
    tb = parse_bounds(tl["bounds"])
    col_w = (tb[2] - tb[0]) // 3
    await tap(serial, (tb[0] + col_w // 2, tb[1] + col_w // 2), "first trial reel tile")

    # 4) Share (retry until share sheet appears)
    share_open = False
    for _ in range(4):
        nodes = await read_ui(serial)
        if _by_text(nodes, "Copy link") or _by_rid(nodes, "search_edit_text"):
            share_open = True
            break
        sb = _by_rid(nodes, "direct_share_button")
        await tap(serial, _center(sb), "Share button")
    if not share_open:
        print("  [fail] share sheet did not open")
        return None

    # 5) Copy link
    nodes = await read_ui(serial)
    await tap(serial, _center(_by_text(nodes, "Copy link")), "'Copy link'")

    # 6) paste into search box and read the URL
    nodes = await read_ui(serial)
    await tap(serial, _center(_by_rid(nodes, "search_edit_text")), "search box (focus)")
    await shell(serial, "input keyevent 279", timeout=10)  # KEYCODE_PASTE
    await asyncio.sleep(0.8)
    nodes = await read_ui(serial)
    sf = _by_rid(nodes, "search_edit_text")
    url = clean_reel_url(node_text(sf) if sf else "")
    if not url:
        for n in nodes:
            url = clean_reel_url(node_text(n))
            if url:
                break
    return url


async def main():
    serial = sys.argv[1]
    url = await capture(serial)
    print(f"\nCAPTURED URL: {url}")
    return 0 if url else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
