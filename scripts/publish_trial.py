#!/usr/bin/env python3
"""Deterministic Trial-Reel publisher -- NO MobileRun agent, NO LLM.

Drives the Instagram publish flow with plain adb + the on-device Mobilerun Portal
content provider (the only screen reader available here; native ``uiautomator``
is killed on these farm devices). Taps with raw ``input tap``; enters the caption
via the Portal keyboard in ONE base64 call (emoji-safe).

PATH A (primary): Profile -> Professional dashboard -> "Trial reels" ->
"Create trial reel" -> newest gallery video -> Next -> caption -> Share -> confirm
on ``trials_list``. Entering via "Create trial reel" makes the post a GENUINE
Trial reel (banner "This is a trial reel..." OR an "Audience: Trial" row) with NO
fragile toggle, and it lands in ``trials_list`` so verification finds it.

OBSERVABILITY: every navigation step declares its TARGET screen and verifies it
was reached. If a target is not reached after retries, the step records a
DEVIATION -- a structured trajectory event plus a full screen dump at the exact
failure point -- so interface differences across devices can be diagnosed and
fixed. Link capture is MULTI-ROUTE (dashboard route, then Reels-tab route).

Usage:
  python scripts/publish_trial.py <serial> "<caption>" [--no-share]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import selectors
import sys
import time

sys.path.insert(0, ".")

from src.worker.agent_runner.custom_tools import _parse_portal_state
from src.worker.tools._adb import shell
from src.worker.tools._ui import node_resource_id, node_text, parse_bounds
from src.worker.tools.instagram import capture_trial_reel_link

SETTLE = 1.2
IG = "com.instagram.android"
_KB_IME = "com.mobilerun.portal/.input.MobilerunKeyboardIME"
# Overlay-less keyboard (no on-screen keys) used by the farm. The normal LatinIME
# draws a full keyboard that COVERS the composer Share button, so a raw input-tap
# lands on the keyboard, not Share ("Share did not register"). AdbKeyboard has no
# overlay -> Share is always tappable, and the Portal caption insert is unaffected
# by which IME is active. We make this the active IME at the start of each run.
ADB_KEYBOARD = "com.genfarmer.uiautomator/.AdbKeyboard"
# Curly apostrophe used by the system "Don't allow" button label (U+2019).
_DENY_CURLY = "Don’t allow"

HUMANIZE = os.environ.get("HUMANIZE", "1").strip().lower() not in ("0", "false", "no", "")
ACTION_MIN = float(os.environ.get("ACTION_DELAY_MIN", "7"))
ACTION_MAX = float(os.environ.get("ACTION_DELAY_MAX", "15"))
TRAJ_ROOT = os.path.join("trajectories", "scripted")


def _action_delay() -> float:
    return random.uniform(ACTION_MIN, ACTION_MAX) if HUMANIZE else SETTLE


# ---------------------------------------------------------------------------
# low-level UI helpers
# ---------------------------------------------------------------------------
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
            if best is None:
                best = n
            else:
                a = lambda m: (lambda b: (b[2]-b[0])*(b[3]-b[1]))(parse_bounds(m["bounds"]))
                best = min((best, n), key=a)
    return best


def _label(n):
    if not n:
        return "?"
    t = node_text(n).strip()
    return t[:30] if t else (node_resource_id(n).split("/")[-1] or "?")


def _clean_reel_url(text):
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


# ---------------------------------------------------------------------------
# Screen detection -- name the screen we're actually on (for logging + targets)
# Ordered specific -> general; detect_screen returns the first match.
# ---------------------------------------------------------------------------
def _first_gallery_thumb(nodes):
    cands = [n for n in nodes
             if node_resource_id(n).endswith("gallery_grid_item_thumbnail") and parse_bounds(n.get("bounds"))]
    if cands:
        return min(cands, key=lambda n: (lambda b: (b[1], b[0]))(parse_bounds(n["bounds"])))
    return _by_text(nodes, "Video thumbnail")


SCREENS = {
    "login":                  lambda ns: bool(_by_text(ns, "Log in", exact=True) or _by_text(ns, "Sign up", exact=True)),
    "permission_dialog":      lambda ns: bool(_by_rid(ns, "permission_message")
                                              or _by_rid(ns, "permission_deny_and_dont_ask_again_button")
                                              or _by_rid(ns, "permission_deny_button")),
    "download_privacy_nux":   lambda ns: bool(_by_rid(ns, "clips_download_privacy_nux_button")),
    "igds_interstitial":      lambda ns: bool(_by_rid(ns, "igds_headline_primary_action_button")),
    "share_sheet":            lambda ns: bool(_by_text(ns, "Copy link") or _by_rid(ns, "search_edit_text")),
    "composer":               lambda ns: bool(_by_rid(ns, "caption_input_text_view")),
    "clips_editor":           lambda ns: bool(_by_rid(ns, "clips_right_action_button") or _by_text(ns, "Edit video")),
    "gallery":                lambda ns: bool(_by_rid(ns, "gallery_recycler_view") or _first_gallery_thumb(ns)),
    "trials_list":            lambda ns: bool(_by_rid(ns, "trials_list")),
    "professional_dashboard": lambda ns: bool(_by_text(ns, "Your tools") or _by_text(ns, "Best practices")),
    "profile":                lambda ns: bool(_by_rid(ns, "profile_tab_layout") or _by_text(ns, "Professional dashboard")),
    "home_feed":              lambda ns: bool(_by_rid(ns, "feed_tab") and not _by_rid(ns, "profile_tab_layout")),
}


def detect_screen(nodes) -> str:
    for name, pred in SCREENS.items():
        try:
            if pred(nodes):
                return name
        except Exception:
            pass
    return "unknown"


# ---------------------------------------------------------------------------
# Trajectory logger -- one JSONL per run + screen dumps on deviation
# ---------------------------------------------------------------------------
class Traj:
    def __init__(self, serial: str, *, tag: str = ""):
        safe = serial.replace(":", "_").replace(".", "_")
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.serial = serial
        self.dir = os.path.join(TRAJ_ROOT, f"{ts}_{safe}{('_' + tag) if tag else ''}")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "trajectory.jsonl")
        self.seq = 0
        self.deviations = 0

    def log(self, event: str, **kw):
        self.seq += 1
        rec = {"seq": self.seq, "ts": round(time.time(), 3), "serial": self.serial, "event": event}
        rec.update(kw)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return rec

    def dump_screen(self, nodes, tag: str) -> str:
        lines = [f"# {self.serial}  screen={detect_screen(nodes)}  nodes={len(nodes)}"]
        for n in nodes:
            b = parse_bounds(n.get("bounds"))
            if not b:
                continue
            rid = node_resource_id(n).split("/")[-1]
            t = node_text(n)
            if rid or t.strip():
                lines.append(f"y={b[1]:>4} x={b[0]:>4} rid={rid!r:30} text={t[:50]!r}")
        p = os.path.join(self.dir, f"dump_{self.seq:02d}_{tag}.txt")
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass
        return p

    def deviation(self, step: str, nodes, *, note: str = "") -> str:
        self.deviations += 1
        dump = self.dump_screen(nodes, step.replace("/", "_"))
        self.log("DEVIATION", step=step, screen=detect_screen(nodes), note=note, dump=dump)
        print(f"  [DEVIATION] {self.serial} step={step!r} on screen={detect_screen(nodes)} (dump: {dump})")
        return dump


async def tap(serial, xy, label="", *, human=True):
    if not xy:
        print(f"  [skip] {label}: no target")
        return False
    await shell(serial, f"input tap {int(xy[0])} {int(xy[1])}", timeout=10)
    d = _action_delay() if human else SETTLE
    print(f"  tapped {label} at {xy}" + (f"  (+{d:.1f}s)" if (human and HUMANIZE) else ""))
    await asyncio.sleep(d)
    return True


async def _swipe_up(serial):
    await shell(serial, "input swipe 540 1400 540 700 350", timeout=10)
    await asyncio.sleep(SETTLE)


async def _swipe_down(serial):
    await shell(serial, "input swipe 540 700 540 1500 350", timeout=10)
    await asyncio.sleep(2.0)


async def _on_launcher(nodes) -> bool:
    return bool(_by_rid(nodes, "apps_list_view") or _by_rid(nodes, "search_container_all_apps")
                or _by_rid(nodes, "workspace"))


async def _dismiss_blockers(serial, nodes, traj=None) -> bool:
    """Dismiss prompts that BLOCK forward navigation, returning True if one was
    handled. Uses SPECIFIC resource-ids (never blind 'Continue'/'OK' taps) so it
    can't mis-fire on a real target screen:
      * Android runtime permission prompt -> Don't allow (a gallery post needs no
        mic; deny-and-don't-ask-again stops it recurring on the device);
      * reels download-privacy NUX ("Others can now download...") -> Continue
        (appears between Next and the composer)."""
    deny = (_by_rid(nodes, "permission_deny_and_dont_ask_again_button")
            or _by_rid(nodes, "permission_deny_button")
            or _by_text(nodes, "Don't allow", exact=True)
            or _by_text(nodes, _DENY_CURLY, exact=True))  # curly apostrophe variant
    if deny:
        msg = _by_rid(nodes, "permission_message")
        if traj:
            traj.log("permission_deny", message=(node_text(msg)[:60] if msg else ""), on=_label(deny))
        print(f"  [permission] {node_text(msg) if msg else 'prompt'} -> Don't allow")
        await tap(serial, _center(deny), "Don't allow (permission)", human=False)
        return True
    nux = _by_rid(nodes, "clips_download_privacy_nux_button")
    if nux:
        if traj:
            traj.log("nux_dismiss", on="clips_download_privacy_nux/Continue")
        print("  [nux] download-privacy -> Continue")
        await tap(serial, _center(nux), "download-privacy NUX (Continue)", human=False)
        return True
    # IG design-system headline interstitial, e.g. "Trial reels need more time to
    # get views" (shows on the trials list during capture) -> primary action
    # ("Got it" / "Continue") dismisses it.
    hl = _by_rid(nodes, "igds_headline_primary_action_button")
    if hl:
        head = _by_rid(nodes, "igds_headline_headline")
        if traj:
            traj.log("interstitial_dismiss", headline=(node_text(head)[:60] if head else ""), on=_label(hl))
        print(f"  [interstitial] {node_text(head) if head else 'igds headline'} -> {_label(hl)}")
        await tap(serial, _center(hl), "igds headline primary action", human=False)
        return True
    return False


async def _open_clean(serial, traj=None):
    """Force-stop + relaunch Instagram into a known state. Self-heals from a stale
    permission dialog or a drop to the home screen (e.g. left over when a prior run
    died mid prompt): deny the prompt, then relaunch until an IG screen shows."""
    for _ in range(3):
        await shell(serial, f"am force-stop {IG}", timeout=15)
        await asyncio.sleep(1)
        await shell(serial, f"monkey -p {IG} -c android.intent.category.LAUNCHER 1", timeout=15)
        await asyncio.sleep(5)
        nodes = await read_ui(serial)
        if await _dismiss_blockers(serial, nodes, traj):
            await asyncio.sleep(2)
            continue  # denying may drop to home -> relaunch IG
        if await _on_launcher(nodes):
            if traj:
                traj.log("open_clean_relaunch", note="on launcher after open -- retrying")
            continue
        return


# ---------------------------------------------------------------------------
# Generic instrumented navigation: tap toward a target SCREEN, verify arrival,
# log every attempt, and record a DEVIATION (with dump) if it never arrives.
# ---------------------------------------------------------------------------
async def _navigate(serial, traj, *, step, find, target, tries=4, human=True):
    """Returns (reached: bool, nodes). find(nodes)->element to tap;
    target(nodes)->True once the destination screen is reached. Blocker prompts
    (permissions, download-privacy NUX) are dismissed WITHOUT consuming a tap."""
    attempt = 0
    while attempt < tries:
        nodes = await read_ui(serial)
        cur = detect_screen(nodes)
        if target(nodes):
            traj.log("step_ok", step=step, attempt=attempt, screen=cur)
            return True, nodes
        if await _dismiss_blockers(serial, nodes, traj):
            continue
        el = find(nodes)
        if el is None:
            traj.log("step_wait", step=step, attempt=attempt, screen=cur, note="target element not found")
            attempt += 1
            await asyncio.sleep(SETTLE)
            continue
        xy = _center(el)
        traj.log("step_tap", step=step, attempt=attempt, screen=cur, xy=list(xy) if xy else None, on=_label(el))
        await tap(serial, xy, step, human=human)
        attempt += 1
    nodes = await read_ui(serial)
    if target(nodes):
        traj.log("step_ok", step=step, attempt=tries, screen=detect_screen(nodes))
        return True, nodes
    traj.deviation(step, nodes, note="target screen not reached after retries")
    return False, nodes


async def _reach_trials_list(serial, traj, *, human=True, tries=6):
    """Scroll the Professional dashboard until the 'Trial reels' row is visible,
    tap it, and verify we land on trials_list. Logs scrolls + a deviation."""
    for attempt in range(tries):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "trials_list"):
            traj.log("step_ok", step="trials_list", attempt=attempt, screen="trials_list")
            return True, nodes
        if await _dismiss_blockers(serial, nodes, traj):
            continue
        tr = _by_text(nodes, "Trial reels", exact=True) or _by_text(nodes, "Trial reels")
        if tr and (parse_bounds(tr["bounds"]) or [0, 0, 0, 9999])[3] < 1820:
            xy = _center(tr)
            traj.log("step_tap", step="trials_list", attempt=attempt, screen=detect_screen(nodes),
                     xy=list(xy) if xy else None, on="Trial reels")
            await tap(serial, xy, "Trial reels", human=human)
            nodes = await read_ui(serial)
            if _by_rid(nodes, "trials_list"):
                traj.log("step_ok", step="trials_list", attempt=attempt, screen="trials_list")
                return True, nodes
        traj.log("step_scroll", step="trials_list", attempt=attempt, screen=detect_screen(nodes))
        await _swipe_up(serial)
    nodes = await read_ui(serial)
    if _by_rid(nodes, "trials_list"):
        return True, nodes
    traj.deviation("trials_list", nodes, note="Trial reels row not found / list not reached")
    return False, nodes


async def _enter_caption(serial, caption):
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
    nodes = await read_ui(serial)
    fld = _by_rid(nodes, "caption_input_text_view")
    txt = node_text(fld) if fld else ""
    landed = bool(txt) and "write a caption" not in txt.lower()
    return landed, (txt[:40] if txt else "(empty)")


async def _ensure_adb_keyboard(serial, traj=None):
    """Make the overlay-less AdbKeyboard the active IME (if enabled on the device),
    so the composer keyboard never covers the Share button. No-op if it's already
    active or not available."""
    try:
        enabled = await shell(serial, "ime list -s", timeout=10)
        if ADB_KEYBOARD.split("/")[0] not in (enabled or ""):
            return
        cur = await shell(serial, "settings get secure default_input_method", timeout=10)
        if ADB_KEYBOARD in (cur or ""):
            return
        await shell(serial, f"ime set {ADB_KEYBOARD}", timeout=10)
        if traj:
            traj.log("ime_set", to="AdbKeyboard", was=(cur or "").strip())
        print(f"  [ime] switched to AdbKeyboard (was {(cur or '').strip()})")
        await asyncio.sleep(1)
    except Exception:
        pass


async def _hide_ime(serial):
    # AdbKeyboard (set at run start) has no overlay, so the Share button is never
    # covered. Belt-and-suspenders: if some soft keyboard is still shown, dismiss
    # it with BACK (consumed by the IME when up; does not leave the screen).
    try:
        shown = await shell(serial, "dumpsys input_method | grep mInputShown", timeout=10)
        if "mInputShown=true" in (shown or ""):
            await shell(serial, "input keyevent 4", timeout=10)
            await asyncio.sleep(1)
    except Exception:
        pass


def _is_trial_composer(nodes) -> bool:
    """Confirm the composer is a TRIAL composer (Path A) -- via the top banner OR
    the 'Audience: Trial' row (layout differs across devices)."""
    return bool(_by_text(nodes, "trial reel") or _by_text(nodes, "Trial", exact=True))


def _fail(traj, stage, detail=""):
    traj.log("publish_fail", stage=stage, detail=detail)
    return {"ok": False, "stage": stage, "detail": detail, "traj": traj.dir, "deviations": traj.deviations}


# ---------------------------------------------------------------------------
# PUBLISH (Path A, instrumented)
# ---------------------------------------------------------------------------
async def publish(serial: str, caption: str, *, no_share: bool = False, traj: "Traj | None" = None) -> dict:
    traj = traj or Traj(serial, tag="publish")
    traj.log("publish_start", caption_len=len(caption), humanize=HUMANIZE, no_share=no_share)
    await _open_clean(serial, traj)
    await _ensure_adb_keyboard(serial, traj)  # overlay-less IME so Share isn't covered

    ok, _ = await _navigate(
        serial, traj, step="profile",
        find=lambda ns: _by_text(ns, "Profile", min_y=1550),
        target=lambda ns: bool(_by_text(ns, "Professional dashboard") or _by_rid(ns, "trials_list")),
    )
    if not ok:
        return _fail(traj, "profile")

    ok, _ = await _navigate(
        serial, traj, step="professional_dashboard",
        find=lambda ns: _by_text(ns, "Professional dashboard"),
        target=lambda ns: bool(_by_text(ns, "Your tools") or _by_rid(ns, "trials_list")),
    )
    if not ok:
        return _fail(traj, "professional_dashboard")

    ok, _ = await _reach_trials_list(serial, traj)
    if not ok:
        return _fail(traj, "trials_list")

    ok, _ = await _navigate(
        serial, traj, step="create_trial_reel",
        find=lambda ns: (_by_rid(ns, "bb_primary_action_container")
                         or _by_rid(ns, "empty_state_create_new_trial")
                         or _by_text(ns, "Create trial reel")
                         or _by_text(ns, "Create new trial reel")),
        target=lambda ns: bool(_by_rid(ns, "gallery_recycler_view") or _first_gallery_thumb(ns)),
        tries=6,
    )
    if not ok:
        return _fail(traj, "gallery")

    # pick newest gallery video (top-left tile)
    nodes = await read_ui(serial)
    thumb = _first_gallery_thumb(nodes)
    if not thumb:
        traj.deviation("pick_video", nodes, note="no gallery thumbnail")
        return _fail(traj, "pick_video")
    traj.log("step_tap", step="pick_video", xy=list(_center(thumb)), on=_label(thumb))
    await tap(serial, _center(thumb), "newest video")

    ok, _ = await _navigate(
        serial, traj, step="next_editor",
        find=lambda ns: (_by_rid(ns, "clips_right_action_button")
                         or _by_rid(ns, "drawer_next_button_layout")
                         or _by_text(ns, "Next", exact=True)),
        target=lambda ns: bool(_by_rid(ns, "caption_input_text_view")),
        tries=6,
    )
    if not ok:
        return _fail(traj, "composer")

    # caption (real text, pasted whole via Portal keyboard)
    landed, capinfo = await _enter_caption(serial, caption)
    traj.log("caption", landed=landed, info=capinfo)
    print(f"  caption landed={landed} ({capinfo})")
    if not landed:
        traj.deviation("caption", await read_ui(serial), note=f"caption did not land: {capinfo}")
        return _fail(traj, "caption", detail=capinfo)

    # sanity: confirm this is a TRIAL composer (banner OR 'Audience: Trial' row)
    nodes = await read_ui(serial)
    is_trial = _is_trial_composer(nodes)
    traj.log("trial_check", is_trial=is_trial)
    if not is_trial:
        # record it -- composer may differ on this device (do not block; Path A
        # entered via "Create trial reel", so the post is a trial regardless)
        traj.dump_screen(nodes, "no_trial_marker")

    await _hide_ime(serial)

    if no_share:
        traj.log("dry_run_stop")
        print("  [dry-run] caption validated; STOPPING before Share (no publish)")
        return {"ok": True, "stage": "dry-run", "detail": "stopped before Share",
                "traj": traj.dir, "deviations": traj.deviations}

    # Share -- composer Share button only
    await _hide_ime(serial)
    for attempt in range(4):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "trials_list"):
            break
        sb = _by_rid(nodes, "share_button")
        if not sb:
            break
        traj.log("step_tap", step="share", attempt=attempt, screen=detect_screen(nodes), xy=list(_center(sb)))
        await tap(serial, _center(sb), "Share")

    nodes = await read_ui(serial)
    published = bool(_by_rid(nodes, "trials_list")
                     or not (_by_rid(nodes, "share_button") or _by_rid(nodes, "caption_input_text_view")))
    screen = detect_screen(nodes)
    traj.log("publish_result", ok=published, screen=screen)
    if not published:
        traj.deviation("share", nodes, note="Share did not register")
        return _fail(traj, "share")
    return {"ok": True, "stage": "share",
            "detail": "on trials_list" if _by_rid(nodes, "trials_list") else "activity changed",
            "traj": traj.dir, "deviations": traj.deviations}


# ---------------------------------------------------------------------------
# LINK CAPTURE (multi-route, instrumented)
# ---------------------------------------------------------------------------
async def _copy_link_from_trials_list(serial, traj) -> str | None:
    nodes = await read_ui(serial)
    # an interstitial ("Trial reels need more time to get views") can cover the
    # list right after arrival — clear it before looking for the tile.
    for _ in range(3):
        if await _dismiss_blockers(serial, nodes, traj):
            nodes = await read_ui(serial)
        else:
            break
    if _by_rid(nodes, "trials_empty_state_title"):
        traj.log("capture_refresh", note="trials list empty -- pull to refresh")
        await _swipe_down(serial)
        nodes = await read_ui(serial)
    tl = _by_rid(nodes, "trials_list")
    if not tl:
        traj.deviation("capture/tile", nodes, note="trials_list missing for tile open")
        return None
    tb = parse_bounds(tl["bounds"])
    col_w = (tb[2] - tb[0]) // 3
    await tap(serial, (tb[0] + col_w // 2, tb[1] + col_w // 2), "newest trial tile", human=False)

    opened = False
    for _ in range(4):
        nodes = await read_ui(serial)
        if _by_text(nodes, "Copy link") or _by_rid(nodes, "search_edit_text"):
            opened = True
            break
        sb = _by_rid(nodes, "direct_share_button") or _by_rid(nodes, "share_button")
        await tap(serial, _center(sb), "Share (tile)", human=False)
    if not opened:
        traj.deviation("capture/share_sheet", await read_ui(serial), note="share sheet did not open")
        return None

    nodes = await read_ui(serial)
    await tap(serial, _center(_by_text(nodes, "Copy link")), "Copy link", human=False)
    nodes = await read_ui(serial)
    await tap(serial, _center(_by_rid(nodes, "search_edit_text")), "search box (focus)", human=False)
    await shell(serial, "input keyevent 279", timeout=10)
    await asyncio.sleep(0.8)
    nodes = await read_ui(serial)
    sf = _by_rid(nodes, "search_edit_text")
    url = _clean_reel_url(node_text(sf) if sf else "")
    if not url:
        for n in nodes:
            url = _clean_reel_url(node_text(n))
            if url:
                break
    return url


async def capture_link(serial, traj: "Traj | None" = None) -> tuple[str | None, str | None]:
    """Capture the newest Trial reel's public link via multiple routes.
    Returns (url, route) or (None, None). Each route logs its outcome."""
    traj = traj or Traj(serial, tag="capture")

    # Route 1 -- Professional dashboard -> Trial reels -> trials_list
    traj.log("capture_route_start", route="dashboard")
    try:
        await _open_clean(serial, traj)
        ok, _ = await _navigate(
            serial, traj, step="cap/profile",
            find=lambda ns: _by_text(ns, "Profile", min_y=1550),
            target=lambda ns: bool(_by_text(ns, "Professional dashboard") or _by_rid(ns, "trials_list")),
            human=False,
        )
        if ok:
            await _navigate(
                serial, traj, step="cap/dashboard",
                find=lambda ns: _by_text(ns, "Professional dashboard"),
                target=lambda ns: bool(_by_text(ns, "Your tools") or _by_rid(ns, "trials_list")),
                human=False,
            )
            ok, _ = await _reach_trials_list(serial, traj, human=False)
            if ok:
                url = await _copy_link_from_trials_list(serial, traj)
                if url:
                    traj.log("capture_route_ok", route="dashboard", url=url)
                    return url, "dashboard"
    except Exception as e:
        traj.log("capture_route_error", route="dashboard", error=str(e))
    traj.log("capture_route_fail", route="dashboard")

    # Route 2 -- Reels sub-tab -> "Drafts and trial reels" tile
    traj.log("capture_route_start", route="reels_tab")
    try:
        await _open_clean(serial, traj)
        url = await capture_trial_reel_link(serial, lambda: read_ui(serial))
        if url:
            traj.log("capture_route_ok", route="reels_tab", url=url)
            return url, "reels_tab"
        traj.deviation("cap/reels_tab", await read_ui(serial), note="reels-tab route returned no url")
    except Exception as e:
        traj.log("capture_route_error", route="reels_tab", error=str(e))
    traj.log("capture_route_fail", route="reels_tab")

    return None, None


# ---------------------------------------------------------------------------
async def _dump_screen(serial):
    nodes = await read_ui(serial)
    print(f"--- screen now ({detect_screen(nodes)}): {len(nodes)} nodes ---")
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
