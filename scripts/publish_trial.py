#!/usr/bin/env python3
"""Deterministic Trial-Reel publisher — NO MobileRun agent, NO LLM.

Drives the Instagram publish flow with plain adb:
  * reads the screen via the on-device Mobilerun Portal content provider
    (``content://com.mobilerun.portal/state``) — the only screen reader available
    here (native ``uiautomator`` is killed on these farm devices);
  * taps with raw ``input tap`` (the Portal /tap endpoint is not needed);
  * enters the caption via the Portal keyboard content provider in ONE call
    (whole string, base64 → emoji-safe — NOT per-character, which does not land);
  * confirms by finding the Trial-reels list.

This replaces ``MobileUIAutomationStep``'s agent for the publish step. It still
uses the on-device Portal app for reads/keyboard (unavoidable while uiautomator
is blocked), but drops the MobileRun python agent + Gemini entirely. Because it
is fast and deterministic (~a dozen quick reads), it should not trip the Portal
a11y "service unavailable" crash that the slow agent triggers.

Path C (proven by the action corpus): Profile → Create New → Create new reel →
newest gallery video → Next → caption → Trial toggle → Share → confirm.

Usage:
  python scripts/publish_trial.py <serial> "<caption>"
  (the video must already be pushed to the gallery — VideoPreparationStep does that)
"""
from __future__ import annotations

import asyncio
import base64
import selectors
import sys

sys.path.insert(0, ".")

from src.worker.agent_runner.custom_tools import _parse_portal_state
from src.worker.tools._adb import shell
from src.worker.tools._ui import node_resource_id, node_text, parse_bounds

SETTLE = 1.2
IG = "com.instagram.android"
_KB_IME = "com.mobilerun.portal/.input.MobilerunKeyboardIME"


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


def _by_text(nodes, needle, *, exact=False, contains_ci=True, min_y=None, max_y=None):
    nl = needle.lower()
    best = None
    for n in nodes:
        t = node_text(n)
        b = parse_bounds(n.get("bounds"))
        if not b:
            continue
        cy = (b[1] + b[3]) // 2
        if min_y is not None and cy < min_y:
            continue
        if max_y is not None and cy > max_y:
            continue
        hit = (t.strip().lower() == nl) if exact else (nl in t.lower() if contains_ci else needle in t)
        if hit:
            # prefer the smallest matching element
            if best is None:
                best = n
            else:
                a = lambda m: (lambda b: (b[2]-b[0])*(b[3]-b[1]))(parse_bounds(m["bounds"]))
                best = min((best, n), key=a)
    return best


async def tap(serial, xy, label=""):
    if not xy:
        print(f"  [skip] {label}: no target")
        return False
    await shell(serial, f"input tap {int(xy[0])} {int(xy[1])}", timeout=10)
    print(f"  tapped {label} at {xy}")
    await asyncio.sleep(SETTLE)
    return True


async def _open_clean(serial):
    await shell(serial, f"am force-stop {IG}", timeout=15)
    await asyncio.sleep(1)
    await shell(serial, f"monkey -p {IG} -c android.intent.category.LAUNCHER 1", timeout=15)
    await asyncio.sleep(5)


async def _tap_until(serial, find, label, *, tries=4, gone=None):
    """Tap the element returned by find(nodes) until it disappears / gone(nodes)."""
    for _ in range(tries):
        nodes = await read_ui(serial)
        if gone and gone(nodes):
            return True, nodes
        n = find(nodes)
        if n is None:
            await asyncio.sleep(SETTLE)
            continue
        await tap(serial, _center(n), label)
    nodes = await read_ui(serial)
    return (gone(nodes) if gone else True), nodes


async def _enter_caption(serial, caption):
    """Focus the caption field and paste the whole caption via the Portal keyboard."""
    nodes = await read_ui(serial)
    fld = _by_rid(nodes, "caption_input_text_view")
    if not fld:
        return False, "caption field not found"
    await tap(serial, _center(fld), "caption field (focus)")
    b64 = base64.b64encode(caption.encode("utf-8")).decode()
    await shell(
        serial,
        f'content insert --uri "content://com.mobilerun.portal/keyboard/input" '
        f'--bind base64_text:s:"{b64}" --bind clear:s:"false"',
        timeout=20,
    )
    await asyncio.sleep(1.5)
    # verify it landed (field no longer shows placeholder, has our prefix)
    nodes = await read_ui(serial)
    fld = _by_rid(nodes, "caption_input_text_view")
    txt = node_text(fld) if fld else ""
    landed = bool(txt) and "write a caption" not in txt.lower()
    return landed, (txt[:40] if txt else "(empty)")


async def _hide_ime(serial):
    # Mobilerun Keyboard ignores focus loss; disabling the IME drops the overlay.
    try:
        await shell(serial, f"ime disable {_KB_IME}", timeout=10)
        await asyncio.sleep(1)
    except Exception:
        pass


async def _clear_dialogs(serial, *, tries=3):
    """Dismiss known interstitials that pop up mid-flow (drafts, NUX, prompts).

    Returns True if it dismissed anything. Each handler taps the button that
    advances toward publishing (discard draft, accept NUX, skip prompt)."""
    cleared = False
    for _ in range(tries):
        nodes = await read_ui(serial)
        el = (
            _by_text(nodes, "Start new video", exact=True)            # "Keep editing your draft?"
            or _by_rid(nodes, "clips_download_privacy_nux_button")    # download-privacy NUX
            or _by_text(nodes, "Not now", exact=True)                 # generic prompts
            or _by_text(nodes, "Skip", exact=True)
        )
        if not el:
            return cleared
        await tap(serial, _center(el), "dismiss dialog")
        cleared = True
    return cleared


async def publish(serial: str, caption: str, *, no_share: bool = False) -> dict:
    await _open_clean(serial)

    # 1) Profile tab — strictly the bottom-nav tab (large y), not a stray "Profile"
    ok, nodes = await _tap_until(
        serial,
        lambda ns: _by_text(ns, "Profile", min_y=1550),
        "Profile tab",
        gone=lambda ns: bool(_by_rid(ns, "action_bar_title")),
    )

    # 2) Create New (+ top-left). gone == the create menu showing "Create new reel"
    # (do NOT match "Reel"/"REEL" — it substring-hits the "Reels" tab on profile).
    await _tap_until(
        serial, lambda ns: _by_text(ns, "Create New"), "Create New",
        gone=lambda ns: bool(_by_text(ns, "Create new reel")),
    )

    # 3) Create new reel
    await _tap_until(
        serial, lambda ns: _by_text(ns, "Create new reel"),
        "Create new reel",
        gone=lambda ns: bool(_by_text(ns, "Video thumbnail")),
    )

    # 3b) "Keep editing your draft?" → Start new video (drafts pile up from prior
    # partial runs); plus any other interstitial before the gallery.
    await _clear_dialogs(serial)

    # 4) newest gallery video (first thumbnail)
    nodes = await read_ui(serial)
    thumb = _by_text(nodes, "Video thumbnail")
    if not thumb:
        # fall back: first clickable tile in the gallery grid area
        thumb = next((n for n in nodes if "thumbnail" in node_text(n).lower() and parse_bounds(n.get("bounds"))), None)
    if thumb:
        await tap(serial, _center(thumb), "newest video")

    # 5) Next (through editor; not the chevron)
    await _tap_until(
        serial, lambda ns: _by_rid(ns, "drawer_next_button_layout") or _by_text(ns, "Next", exact=True),
        "Next",
        gone=lambda ns: bool(_by_rid(ns, "caption_input_text_view")),
    )

    # 5b) dismiss any interstitial NUX dialogs (e.g. clips_download_privacy_nux,
    # "Others can now download…") that appear between Next and the Share screen.
    for _ in range(4):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "caption_input_text_view"):
            break
        cont = (_by_rid(nodes, "clips_download_privacy_nux_button")
                or _by_text(nodes, "Continue", exact=True)
                or _by_text(nodes, "OK", exact=True)
                or _by_text(nodes, "Done", exact=True))
        if cont:
            await tap(serial, _center(cont), "dismiss interstitial")
        else:
            break

    # 6) caption
    landed, capinfo = await _enter_caption(serial, caption)
    print(f"  caption landed={landed} ({capinfo})")
    if not landed:
        return {"ok": False, "stage": "caption", "detail": capinfo}

    # 7) Trial toggle ON
    await _hide_ime(serial)
    nodes = await read_ui(serial)
    toggle = _by_rid(nodes, "toggle") or _by_text(nodes, "Trial")
    if toggle:
        await tap(serial, _center(toggle), "Trial toggle")
    else:
        print("  [warn] Trial toggle not found — may already be a trial composer")

    # DRY-RUN: never publish during testing — a real post must carry the real
    # caption, never a placeholder. Stop here with the caption validated.
    if no_share:
        print("  [dry-run] caption validated; STOPPING before Share (no publish)")
        return {"ok": True, "stage": "dry-run", "detail": "stopped before Share"}

    # 8) Share — tap the SPECIFIC composer Share button, not any stray "Share"
    # text (the post-publish screen has share-to-story etc. that must not be hit).
    await _hide_ime(serial)
    for _ in range(4):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "trials_list") or _by_text(nodes, "Trial reels"):
            break
        sb = _by_rid(nodes, "share_button") or _by_rid(nodes, "direct_share_button")
        if not sb:
            break
        await tap(serial, _center(sb), "Share")

    # 9) confirm
    nodes = await read_ui(serial)
    published = bool(_by_rid(nodes, "trials_list") or _by_text(nodes, "Trial reels")
                     or not (_by_rid(nodes, "share_button") or _by_rid(nodes, "caption_input_text_view")))
    return {"ok": published, "stage": "share", "detail": "on trials_list" if _by_rid(nodes, "trials_list") else "activity changed"}


async def _dump_screen(serial):
    nodes = await read_ui(serial)
    print(f"--- screen now: {len(nodes)} nodes; tappable/text elements: ---")
    for n in nodes:
        b = parse_bounds(n.get("bounds"))
        if not b:
            continue
        rid = node_resource_id(n).split("/")[-1]
        t = node_text(n)
        if rid or t.strip():
            print(f"   y={b[1]:>4} x={b[0]:>4} rid={rid!r:26} text={t[:34]!r}")


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_share = "--no-share" in sys.argv
    serial = args[0]
    caption = args[1] if len(args) > 1 else "(no caption)"
    res = await publish(serial, caption, no_share=no_share)
    print("\nRESULT:", res)
    if not res.get("ok"):
        await _dump_screen(serial)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
