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
import subprocess
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
ACTION_MIN = float(os.environ.get("ACTION_DELAY_MIN", "15"))
ACTION_MAX = float(os.environ.get("ACTION_DELAY_MAX", "30"))
# Per-character caption typing cadence (humanized): a per-run base delay chosen
# once in [CHAR_MIN, CHAR_MAX] (this "typist's" speed) plus +/- CHAR_JITTER per
# keystroke. Seconds.
CHAR_MIN = float(os.environ.get("CHAR_DELAY_MIN", "0.620"))
CHAR_MAX = float(os.environ.get("CHAR_DELAY_MAX", "1.040"))
CHAR_JITTER = float(os.environ.get("CHAR_JITTER", "0.020"))
TRAJ_ROOT = os.path.join("trajectories", "scripted")


def _action_delay() -> float:
    return random.uniform(ACTION_MIN, ACTION_MAX) if HUMANIZE else SETTLE


# ---------------------------------------------------------------------------
# low-level UI helpers
# ---------------------------------------------------------------------------
async def read_ui(serial: str):
    raw = await shell(serial, "content query --uri content://com.mobilerun.portal/state", timeout=15)
    return _parse_portal_state(raw)


# ---------------------------------------------------------------------------
# Accessibility-service health + auto-recovery
# Every screen read goes through the Mobilerun Portal a11y service. When it drops
# (killed under memory pressure, or enabled-but-not-bound after a reboot) the Portal
# returns no a11y_tree and the WHOLE flow goes blind: challenge checks, navigation
# and link capture all read empty. We detect that and rebind the service.
# ---------------------------------------------------------------------------
PORTAL_STATE_URI = "content://com.mobilerun.portal/state"
ACC_SERVICE = "com.mobilerun.portal/com.mobilerun.portal.service.MobilerunAccessibilityService"


def _adb_bin() -> str:
    return os.environ.get("ADB_PATH", "adb")


def _adb_dev(serial, *args, timeout=30):
    return subprocess.run([_adb_bin(), "-s", serial, *args],
                          capture_output=True, text=True, timeout=timeout)


def _adb_global(*args, timeout=30):
    return subprocess.run([_adb_bin(), *args], capture_output=True, text=True, timeout=timeout)


async def a11y_ok(serial: str) -> bool:
    """True iff the Portal returns a BOUND accessibility tree (status success).
    A False here means every UI read will come back empty -> recover before acting."""
    try:
        raw = await shell(serial, f"content query --uri {PORTAL_STATE_URI}", timeout=20)
    except Exception:
        return False
    return ("a11y_tree" in raw) and ('"status":"success"' in raw.replace(" ", ""))


async def recover_accessibility(serial: str, traj=None, *, reconnect_timeout: int = 300) -> bool:
    """Re-enable the Mobilerun a11y service after it has dropped, then verify.

    Per the standing runbook: toggle the secure accessibility setting, REBOOT, and
    -- because WiFi-adb devices do NOT auto-reconnect after a reboot -- reconnect
    with ``adb connect`` after 20s and then every 10s for up to 5 minutes. Finally
    wait for the Portal to rebind and confirm the a11y tree is readable again.
    Returns True iff a11y works afterwards (only then is it safe to continue)."""
    def _log(ev, **kw):
        try:
            if traj:
                traj.log(ev, **kw)
        except Exception:
            pass
        print(f"  [a11y-recover] {serial} {ev}" + (f" {kw}" if kw else ""))

    _log("a11y_recover_start")

    def _toggle():
        # delete + re-add the enabled-services list and flip the master switch:
        # this rebinds the Mobilerun accessibility service cleanly.
        _adb_dev(serial, "shell", "settings", "delete", "secure",
                 "enabled_accessibility_services", timeout=15)
        _adb_dev(serial, "shell", "settings", "put", "secure", "accessibility_enabled", "0", timeout=15)
        time.sleep(1)
        _adb_dev(serial, "shell", "settings", "put", "secure",
                 "enabled_accessibility_services", ACC_SERVICE, timeout=15)
        _adb_dev(serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1", timeout=15)
    try:
        await asyncio.to_thread(_toggle)
    except Exception as e:
        _log("a11y_toggle_error", error=str(e))

    try:                                                # reboot to force a clean rebind
        await asyncio.to_thread(lambda: _adb_dev(serial, "reboot", timeout=20))
    except Exception as e:
        _log("a11y_reboot_error", error=str(e))

    # reconnect: wait 20s, then `adb connect` every 10s up to 5 min. `get-state`
    # alone hangs on a rebooting WiFi-adb device, so always `connect` first.
    _log("a11y_reconnect_wait", seconds=20)
    await asyncio.sleep(20)
    booted = False
    deadline = time.monotonic() + reconnect_timeout
    while time.monotonic() < deadline:
        try:
            await asyncio.to_thread(lambda: _adb_global("connect", serial, timeout=10))
            st = await asyncio.to_thread(lambda: _adb_dev(serial, "get-state", timeout=10))
            if (st.stdout or "").strip() == "device":
                bc = await asyncio.to_thread(
                    lambda: _adb_dev(serial, "shell", "getprop", "sys.boot_completed", timeout=10))
                if (bc.stdout or "").strip() == "1":
                    booted = True
                    break
        except Exception:
            pass
        await asyncio.sleep(10)
    if not booted:
        _log("a11y_recover_no_reconnect")
        return False

    # let the Portal rebind, then verify (binding lags a few seconds after boot)
    _log("a11y_reconnect_ok")
    await asyncio.sleep(25)
    for _ in range(8):
        if await a11y_ok(serial):
            _log("a11y_recover_ok")
            return True
        await asyncio.sleep(5)
    _log("a11y_recover_still_down")
    return False


def _center(n):
    if not n:
        return None
    b = parse_bounds(n.get("bounds"))
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) if b else None


def _jxy(target):
    """A human-ish tap point — NEVER the same pixel twice. For a UI node, a random
    point biased (Gaussian) toward the centre of its bounds but kept well inside it;
    for a raw (x, y) point, a few px of jitter. Returns None if there's no target."""
    if target is None:
        return None
    if isinstance(target, dict):                       # a UI node -> jitter in bounds
        b = parse_bounds(target.get("bounds"))
        if not b:
            return None
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        w, h = max(1, b[2] - b[0]), max(1, b[3] - b[1])
        x = cx + random.gauss(0, w * 0.16)             # ~central 60% of the element
        y = cy + random.gauss(0, h * 0.16)
        x = min(b[2] - 2, max(b[0] + 2, x))            # clamp inside -> never misfire
        y = min(b[3] - 2, max(b[1] + 2, y))
        return (int(x), int(y))
    x, y = target                                      # a raw point -> small jitter
    return (int(x + random.gauss(0, 4)), int(y + random.gauss(0, 4)))


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
    # the FIRST media tile that is NOT the "Open camera" cell — that cell opens the
    # camera, not a video. (The camera tile uses a different rid, but also guard by text.)
    cands = [n for n in nodes
             if node_resource_id(n).endswith("gallery_grid_item_thumbnail")
             and parse_bounds(n.get("bounds"))
             and "camera" not in (node_text(n) + str(n.get("content_desc") or "")).lower()]
    if cands:
        return min(cands, key=lambda n: (lambda b: (b[1], b[0]))(parse_bounds(n["bounds"])))
    return _by_text(nodes, "Video thumbnail")


async def _select_videos_folder(serial, traj=None):
    """In the media picker, switch the album dropdown from 'Recents' to 'Videos' so the
    grid shows ONLY videos. This prevents a stray screenshot/photo (which can sort
    ahead of the pushed video in the mixed 'Recents' view) from being picked as the
    reel. No-op if already on Videos or if the control isn't present."""
    nodes = await read_ui(serial)
    dd = _by_rid(nodes, "gallery_folder_menu_tv") or _by_text(nodes, "Recents")
    if not dd:
        return
    if "video" in node_text(dd).strip().lower():            # already filtered to Videos
        return
    await tap(serial, _jxy(dd), "album dropdown", human=False)
    await asyncio.sleep(1.0)
    nodes = await read_ui(serial)
    vids = _by_text(nodes, "Videos", exact=True) or _by_text(nodes, "Videos")
    if vids:
        if traj:
            traj.log("gallery_filter", folder="Videos")
        await tap(serial, _jxy(vids), "Videos folder", human=False)
        await asyncio.sleep(1.2)
    elif traj:
        traj.log("gallery_filter_missing", note="no 'Videos' entry in album dropdown")


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


# Login challenge / checkpoint / block detection. Hitting any of these means we
# must STOP for this device immediately — never keep tapping (that can escalate a
# checkpoint into a hard lock). Markers are matched against all node text +
# content-desc (case-insensitive). Tuned from the real .50 screen ("Confirm
# you're human to use your account, …") plus the AppCard hard-stop conditions.
class HardStop(Exception):
    def __init__(self, reason: str, marker: str = ""):
        self.reason = reason
        self.marker = marker
        super().__init__(f"{reason}: {marker}")


class TrialLimit(Exception):
    """IG's "You've reached the limit — you've shared the maximum number of trial
    reels allowed" interstitial. The account is trial-rate-limited (not blocked);
    we tap OK and take the device out of the posting loop."""
    def __init__(self, headline: str = ""):
        self.headline = headline
        super().__init__(headline or "trial reels limit reached")


CHALLENGE_MARKERS = (
    "confirm you're human", "confirm you’re human", "confirm it's you", "confirm it’s you",
    "confirm your identity", "help us confirm", "verify it's you", "verify it’s you",
    "we detected unusual", "unusual activity", "suspicious login attempt",
    "enter the code", "we sent a code", "enter security code", "two-factor",
    "your account has been disabled", "account has been disabled",
    "we suspended your account", "account suspended", "your account is suspended",
    "we restrict certain activity", "action blocked",
    "we limit how often", "you're temporarily blocked", "temporarily blocked",
    "tell us if this was you", "was it you",
)
# NOTE: a bare "try again later" is intentionally NOT a marker — it is a generic
# transient/error/connectivity message (e.g. "Something went wrong. Try again
# later.") that caused FALSE blocks. Real action-blocks also carry a stronger marker
# above ("we restrict certain activity" / "action blocked"), so they're still caught.


def _hard_stop_reason(nodes) -> tuple[str, str] | None:
    """Return (reason, marker) if a login/checkpoint/block screen is present."""
    # logged out — require the sign-up affordance too, so a stray "Log in" link
    # elsewhere is not mistaken for the logged-out screen.
    if _by_text(nodes, "Log in", exact=True) and (_by_text(nodes, "Sign up", exact=True)
                                                   or _by_text(nodes, "Create new account")):
        return "logged_out", "Log in / Sign up"
    blob = " ".join((node_text(n) or "") for n in nodes).lower()
    for m in CHALLENGE_MARKERS:
        if m in blob:
            return "login_challenge", m
    return None


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
    # ``xy`` is already a concrete (jittered) point from _jxy(...). Tap as a real
    # touch gesture, not an injected `input tap`: a down->up with a small random
    # hold and 1-3 px of drift so it registers with a duration like a finger, not a
    # synthetic zero-time click.
    if not xy:
        print(f"  [skip] {label}: no target")
        return False
    x, y = int(xy[0]), int(xy[1])
    x2, y2 = x + int(round(random.gauss(0, 1.6))), y + int(round(random.gauss(0, 1.6)))
    hold = random.randint(80, 180)                      # ms — human press duration
    await shell(serial, f"input touchscreen swipe {x} {y} {x2} {y2} {hold}", timeout=10)
    d = _action_delay() if human else SETTLE
    print(f"  tapped {label} at ({x},{y})" + (f"  (+{d:.1f}s)" if (human and HUMANIZE) else ""))
    await asyncio.sleep(d)
    return True


async def _smooth_swipe(serial, x1, y1, x2, y2, dur=None):
    """A slow, finger-like scroll: a touchscreen gesture with a randomized longer
    duration (~0.7-1.0s) and a few px of endpoint jitter, so it reads as a human
    swipe rather than a fast synthetic flick."""
    if dur is None:
        dur = random.randint(700, 980)
    def jx(v):
        return v + int(round(random.gauss(0, 6)))
    await shell(serial, f"input touchscreen swipe {jx(x1)} {y1} {jx(x2)} {y2} {dur}", timeout=10)


async def _swipe_up(serial):
    await _smooth_swipe(serial, 540, 1400, 540, 700)
    await asyncio.sleep(SETTLE)


async def _swipe_down(serial):
    await _smooth_swipe(serial, 540, 700, 540, 1500)
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
        await tap(serial, _jxy(deny), "Don't allow (permission)", human=False)
        return True
    # App-level location-permission prompt (seen on .95/.190): IG shows a screen
    # asking to use location with a "Continue" button; tapping it brings the
    # SYSTEM grant dialog, which the deny block above answers "Don't allow" on the
    # next read. Scope to a location-worded prompt so "Continue" can NEVER fire on
    # a real target screen (and never on the rid-based download-privacy NUX).
    if not _by_rid(nodes, "clips_download_privacy_nux_button"):
        loc_cont = _by_text(nodes, "Continue", exact=True)
        loc_sig = (_by_text(nodes, "your location") or _by_text(nodes, "location services")
                   or _by_text(nodes, "access your location") or _by_text(nodes, "use your location")
                   or _by_text(nodes, "your device’s location") or _by_text(nodes, "your device's location"))
        if loc_cont and loc_sig:
            if traj:
                traj.log("location_continue", on=_label(loc_cont))
            print("  [location] location prompt -> Continue (then Don't allow)")
            await tap(serial, _jxy(loc_cont), "location prompt (Continue)", human=False)
            return True
    # Follow-up permission / education screen after the location flow -> "Skip".
    # Gate on it NOT being a real posting screen so a legitimate step (composer,
    # gallery, editor, share, trials list) can never be skipped by accident.
    if not (_by_rid(nodes, "caption_input_text_view") or _by_rid(nodes, "gallery_recycler_view")
            or _by_rid(nodes, "share_button") or _by_rid(nodes, "trials_list")
            or _by_rid(nodes, "clips_right_action_button")):
        skip = _by_text(nodes, "Skip", exact=True)
        if skip:
            if traj:
                traj.log("permission_skip", on=_label(skip))
            print("  [location] follow-up permission screen -> Skip")
            await tap(serial, _jxy(skip), "permission (Skip)", human=False)
            return True
    nux = _by_rid(nodes, "clips_download_privacy_nux_button")
    if nux:
        if traj:
            traj.log("nux_dismiss", on="clips_download_privacy_nux/Continue")
        print("  [nux] download-privacy -> Continue")
        await tap(serial, _jxy(nux), "download-privacy NUX (Continue)", human=False)
        return True
    # IG design-system headline interstitial, e.g. "Trial reels need more time to
    # get views" (shows on the trials list during capture) -> primary action
    # ("Got it" / "Continue") dismisses it.
    hl = _by_rid(nodes, "igds_headline_primary_action_button")
    if hl:
        head = _by_rid(nodes, "igds_headline_headline")
        body = _by_rid(nodes, "igds_headline_body")
        htext = (node_text(head) if head else "").lower()
        btext = (node_text(body) if body else "").lower()
        # "You've reached the limit -- you've shared the maximum number of trial
        # reels allowed". A trial-reels RATE LIMIT (not a block): tap OK to clear
        # it, then raise so the device leaves the posting loop.
        is_limit = ("reached the limit" in htext or "maximum number of trial reels" in btext
                    or ("trial reels" in btext and "limit" in (htext + btext)))
        if is_limit:
            if traj:
                traj.log("trial_limit", headline=node_text(head)[:80] if head else "", on=_label(hl))
            print(f"  [trial-limit] {node_text(head) if head else 'limit reached'} -> OK, stopping device")
            await tap(serial, _jxy(hl), "trial-limit OK", human=False)
            raise TrialLimit(node_text(head) if head else "")
        if traj:
            traj.log("interstitial_dismiss", headline=(node_text(head)[:60] if head else ""), on=_label(hl))
        print(f"  [interstitial] {node_text(head) if head else 'igds headline'} -> {_label(hl)}")
        await tap(serial, _jxy(hl), "igds headline primary action", human=False)
        return True
    # Soft "We suspect automated behavior on your account…" notice (NOT a real block /
    # checkpoint — it carries a plain Dismiss and lets you keep going). Tapping Dismiss
    # clears it so navigation proceeds instead of stalling on an unknown screen.
    warn = _by_text(nodes, "suspect automated behavior") or _by_text(nodes, "automated behavior on your account")
    if warn:
        dismiss = _by_text(nodes, "Dismiss", exact=True) or _by_text(nodes, "Dismiss")
        if dismiss:
            if traj:
                traj.log("warn_dismiss", note="automated-behavior notice", on="Dismiss")
            print("  [warn] 'automated behavior' notice -> Dismiss")
            await tap(serial, _jxy(dismiss), "automated-behavior warn (Dismiss)", human=False)
            return True
    # "Save your login info?" dialog (offered right after a login / app open). It is
    # NOT a checkpoint -- it just stalls navigation on an unknown screen, which makes
    # Path A bail and fall through to the alt path (and falsely flag accounts whose
    # alt composer lacks a Trial toggle). Decline with "Not now" to keep moving; we
    # never want to persist creds on the device.
    save_login = _by_text(nodes, "Save your login info") or _by_text(nodes, "save the login info for")
    if save_login:
        not_now = _by_text(nodes, "Not now", exact=True) or _by_text(nodes, "Not now")
        if not_now:
            if traj:
                traj.log("save_login_dismiss", note="Save your login info? dialog", on="Not now")
            print("  [save-login] 'Save your login info?' -> Not now")
            await tap(serial, _jxy(not_now), "save-login dialog (Not now)", human=False)
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
        hs = _hard_stop_reason(nodes)
        if hs:
            traj.deviation(f"hard_stop/{step}", nodes, note=f"{hs[0]}: {hs[1]}")
            raise HardStop(*hs)
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
        await tap(serial, _jxy(el), step, human=human)
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
            await tap(serial, _jxy(tr), "Trial reels", human=human)
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


def _on_best_practices(nodes) -> bool:
    """True when the screen's action bar title is 'Best practices' (the dedicated
    Best-practices screen, distinct from the dashboard which only links to it)."""
    t = _by_rid(nodes, "action_bar_title")
    return bool(t and "best practices" in node_text(t).lower())


async def _reach_trials_list_via_best_practices(serial, traj, *, human=False) -> bool:
    """Third path to the trials list (when the dashboard 'Trial reels' row route
    fails): Profile -> Professional dashboard -> 'Best practices' -> 'Trial reels'
    tab -> scroll to the 'Go to trial reels' button -> trials_list."""
    traj.log("bp_path_start")
    await _open_clean(serial, traj)
    ok, _ = await _navigate(
        serial, traj, step="bp/profile",
        find=lambda ns: _by_text(ns, "Profile", min_y=1550),
        target=lambda ns: bool(_by_text(ns, "Professional dashboard") or _by_rid(ns, "trials_list")),
        human=human)
    if not ok:
        return False
    ok, _ = await _navigate(
        serial, traj, step="bp/dashboard",
        find=lambda ns: _by_text(ns, "Professional dashboard"),
        target=lambda ns: bool(_by_text(ns, "Your tools") or _by_text(ns, "Best practices")
                               or _by_rid(ns, "trials_list")),
        human=human)
    if not ok:
        return False
    # Open the dedicated Best-practices screen (tap the 'Best practices' link on the
    # dashboard; arrival = action bar title 'Best practices').
    ok, _ = await _navigate(
        serial, traj, step="bp/open",
        find=lambda ns: _by_text(ns, "Best practices"),
        target=lambda ns: _on_best_practices(ns) or bool(_by_text(ns, "Go to trial reels")),
        human=human)
    if not ok:
        return False
    # Switch to the 'Trial reels' best-practices tab if the button isn't already shown.
    nodes = await read_ui(serial)
    if not _by_text(nodes, "Go to trial reels"):
        tab = _by_text(nodes, "Trial reels", exact=True) or _by_text(nodes, "Trial reels")
        if tab:
            traj.log("bp_trial_reels_tab", on=_label(tab))
            await tap(serial, _jxy(tab), "Best Practices: Trial reels tab", human=human)
            await asyncio.sleep(1.5)
    # Scroll down to the 'Go to trial reels' button and tap it -> trials_list.
    for attempt in range(8):
        nodes = await read_ui(serial)
        btn = _by_text(nodes, "Go to trial reels")
        if btn:
            traj.log("bp_go_to_trial_reels", attempt=attempt)
            await tap(serial, _jxy(btn), "Go to trial reels", human=human)
            await asyncio.sleep(1.6)
            nodes = await read_ui(serial)
            if _by_rid(nodes, "trials_list"):
                traj.log("step_ok", step="bp/trials_list", screen="trials_list")
                return True
            return False
        if await _dismiss_blockers(serial, nodes, traj):
            continue
        await _swipe_up(serial)
    traj.deviation("bp/go_to_trial_reels", await read_ui(serial), note="'Go to trial reels' not found")
    return False


async def _reach_trials_list_any(serial, traj) -> bool:
    """Reach the Trial-reels list trying the dashboard paths in order:
      1) Profile -> Professional dashboard -> 'Trial reels' row;
      2) Profile -> Professional dashboard -> Best practices -> Trial reels ->
         'Go to trial reels'.
    Returns True once on trials_list, else False (caller falls to the alt path)."""
    ok, _ = await _navigate(
        serial, traj, step="profile",
        find=lambda ns: _by_text(ns, "Profile", min_y=1550),
        target=lambda ns: bool(_by_text(ns, "Professional dashboard") or _by_rid(ns, "trials_list")))
    if ok:
        ok, _ = await _navigate(
            serial, traj, step="professional_dashboard",
            find=lambda ns: _by_text(ns, "Professional dashboard"),
            target=lambda ns: bool(_by_text(ns, "Your tools") or _by_rid(ns, "trials_list")))
    if ok:
        ok, _ = await _reach_trials_list(serial, traj)
    if ok:
        return True
    # Path 1 (dashboard 'Trial reels' row) unavailable -> try the Best-practices path.
    traj.log("pathA_unavailable", at="dashboard_row")
    return await _reach_trials_list_via_best_practices(serial, traj)


async def _caption_insert(serial, text, *, clear):
    """Insert text into the focused field via the Portal keyboard content provider
    (base64 -> shell-safe + emoji-safe). clear=True replaces the field content."""
    b64 = base64.b64encode(text.encode("utf-8")).decode()
    await shell(
        serial,
        f'content insert --uri "content://com.mobilerun.portal/keyboard/input" '
        f'--bind base64_text:s:"{b64}" --bind clear:s:"{"true" if clear else "false"}"',
        timeout=20,
    )


def _grapheme_clusters(s):
    """Split a string into user-perceived characters (grapheme clusters) so EMOJI
    are never broken apart: flags (two regional indicators), ZWJ sequences
    (e.g. family / profession emoji), skin-tone modifiers, variation selectors,
    keycaps, and combining marks each stay as ONE unit. Without this, iterating
    code points would split e.g. the Curacao flag into two letter-boxes."""
    def is_ri(c):
        return 0x1F1E6 <= ord(c) <= 0x1F1FF
    def is_ext(c):
        o = ord(c)
        return (c in ("️", "︎", "⃣")    # VS16/VS15, combining keycap
                or 0x1F3FB <= o <= 0x1F3FF             # skin-tone modifiers
                or 0x0300 <= o <= 0x036F               # combining diacritics
                or 0xE0020 <= o <= 0xE007F)            # tag chars (subdivision flags)
    out, i, n = [], 0, len(s)
    while i < n:
        cl = s[i]
        i += 1
        if is_ri(cl) and i < n and is_ri(s[i]):        # regional-indicator flag = 2 RIs
            cl += s[i]
            i += 1
        while i < n:                                   # absorb extenders + ZWJ-joined runs
            if is_ext(s[i]):
                cl += s[i]
                i += 1
            elif s[i] == "‍" and i + 1 < n:       # ZWJ joins the following glyph
                cl += s[i] + s[i + 1]
                i += 2
            else:
                break
        out.append(cl)
    return out


async def _type_caption_humanized(serial, caption, traj=None):
    """Type the caption ONE GRAPHEME AT A TIME (emoji-safe) with a human cadence:
    a per-run base delay in [CHAR_MIN, CHAR_MAX] (this typist's speed), plus
    +/- CHAR_JITTER per keystroke. The first unit clears the field; the rest
    append. Each emoji is inserted whole, so it can never be dropped or split."""
    units = _grapheme_clusters(caption)
    base = random.uniform(CHAR_MIN, CHAR_MAX)
    emoji = sum(1 for u in units if any(ord(c) > 0x2100 for c in u))
    if traj:
        traj.log("type_per_char", units=len(units), chars=len(caption),
                 emoji=emoji, base_ms=round(base * 1000))
    print(f"  typing {len(units)} graphemes ({emoji} emoji) per-unit "
          f"(base {base * 1000:.0f}ms +/-{CHAR_JITTER * 1000:.0f}ms)")
    for i, u in enumerate(units):
        await _caption_insert(serial, u, clear=(i == 0))
        await asyncio.sleep(max(0.03, base + random.uniform(-CHAR_JITTER, CHAR_JITTER)))


def _full_caption_landed(txt, caption) -> bool:
    """True only if the WHOLE caption is present (guards against a partial type)."""
    if not txt or "write a caption" in txt.lower():
        return False
    cap = caption.strip()
    tail = cap[-12:].strip()
    return (bool(tail) and tail in txt) or len(txt) >= len(cap) - 2


async def _enter_caption(serial, caption, traj=None):
    nodes = await read_ui(serial)
    fld = _by_rid(nodes, "caption_input_text_view")
    if not fld:
        return False, "caption field not found"
    await tap(serial, _jxy(fld), "caption field (focus)")

    if HUMANIZE:
        await _type_caption_humanized(serial, caption, traj)
    else:
        await _caption_insert(serial, caption, clear=False)
    await asyncio.sleep(1.2)

    nodes = await read_ui(serial)
    fld = _by_rid(nodes, "caption_input_text_view")
    txt = node_text(fld) if fld else ""
    landed = _full_caption_landed(txt, caption)

    # Correctness guarantee (the hard rule): a real publish must carry the EXACT
    # caption. If per-char typing did not land the whole thing (dropped keystroke,
    # or the a11y text is truncated so we cannot confirm), replace it in one shot
    # with the full caption and accept on a non-placeholder field.
    if not landed:
        if traj:
            traj.log("caption_fallback_oneshot", field_len=len(txt), want_len=len(caption))
        print(f"  [caption] per-char incomplete (field {len(txt)} vs {len(caption)}) -> one-shot insert")
        await _caption_insert(serial, caption, clear=True)
        await asyncio.sleep(1.2)
        nodes = await read_ui(serial)
        fld = _by_rid(nodes, "caption_input_text_view")
        txt = node_text(fld) if fld else ""
        landed = bool(txt) and "write a caption" not in txt.lower()

    return landed, (f"{len(txt)}ch: {txt[:36]}" if txt else "(empty)")


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


class TrialUnavailable(Exception):
    """Raised when NEITHER known path to a trial reel is available on this account
    (no dashboard 'Trial reels' AND no 'Trial' toggle in the reel composer). The
    device should STOP posting -- the account simply can't make trial reels."""
    def __init__(self, detail: str = ""):
        self.detail = detail or "trial reels not enabled"
        super().__init__(self.detail)


# --- alt path helpers: the reel composer's Audience/Trial toggle -----------------
def _row_subtitle(nodes, title_text):
    """The inline_subtitle on the same row as a given title (e.g. Audience->Trial)."""
    t = _by_text(nodes, title_text, exact=True)
    if not t:
        return None
    ty = (parse_bounds(t["bounds"]) or [0, 0, 0, 0])[1]
    for n in nodes:
        if node_resource_id(n).endswith("/inline_subtitle"):
            b = parse_bounds(n.get("bounds"))
            if b and abs(b[1] - ty) < 60:
                return node_text(n).strip()
    return None


def _trial_audience_on(nodes) -> bool:
    """True once the composer's Audience row reads 'Trial' (toggle confirmed on)."""
    return (_row_subtitle(nodes, "Audience") or "").lower() == "trial"


def _trial_toggle_node(nodes):
    """The 'Trial' ToggleButton: rid endswith /toggle on the same row as title 'Trial'."""
    t = _by_text(nodes, "Trial", exact=True)
    if not t:
        return None
    ty = (parse_bounds(t["bounds"]) or [0, 0, 0, 0])[1]
    for n in nodes:
        if node_resource_id(n).endswith("/toggle") and parse_bounds(n.get("bounds")):
            if abs(parse_bounds(n["bounds"])[1] - ty) < 90:
                return n
    return None


async def _enable_trial_toggle(serial, traj) -> bool:
    """Scroll the reel composer to the Audience/Trial row and ENABLE the Trial
    toggle. Tapping it opens a one-time info sheet -> dismiss with 'Close'. Returns
    True once the Audience row reads 'Trial'; False if there is NO Trial toggle
    (account ineligible) so the caller can stop."""
    for _ in range(6):
        nodes = await read_ui(serial)
        if _trial_audience_on(nodes):
            return True
        tg = _trial_toggle_node(nodes)
        if tg:
            await tap(serial, _jxy(tg), "Trial toggle", human=False)
            await asyncio.sleep(1.2)
            nodes = await read_ui(serial)
            close = (_by_rid(nodes, "bb_primary_action_container")
                     or _by_text(nodes, "Close", exact=True))
            if close:
                traj.log("trial_info_sheet_close")
                await tap(serial, _jxy(close), "Close trial info", human=False)
                await asyncio.sleep(1.0)
                nodes = await read_ui(serial)
            return _trial_audience_on(nodes)
        await _swipe_up(serial)               # toggle is below the fold -> scroll down
    return False


async def _alt_share(serial, traj) -> None:
    """Share from the create-reel composer: its button is labelled 'Next' and leads
    to an 'About Reels' sheet whose 'Share' actually publishes."""
    for attempt in range(6):
        nodes = await read_ui(serial)
        if _by_rid(nodes, "trials_list") or _by_text(nodes, "Your reel was shared"):
            return
        nux = _by_rid(nodes, "clips_nux_sheet_share_button")
        if nux:
            traj.log("alt_share", step="about_reels_share", attempt=attempt)
            await tap(serial, _jxy(nux), "About Reels -> Share", human=False)
            await asyncio.sleep(2.5)
            continue
        sb = _by_rid(nodes, "share_button")
        if sb:
            traj.log("alt_share", step="composer_next", attempt=attempt)
            await tap(serial, _jxy(sb), "composer Next", human=False)
            await asyncio.sleep(2.5)
            continue
        return


async def _publish_via_create_reel(serial, caption, traj, *, no_share=False) -> dict:
    """FALLBACK trial path (used when Path A's dashboard 'Trial reels' is absent):
    Profile -> '+' (Create New) -> 'Create new reel' -> gallery -> editor -> composer
    -> caption -> ENABLE the Trial toggle (this is what makes it a trial reel AND the
    eligibility test) -> Share. Raises TrialUnavailable if the composer has no Trial
    toggle (the account simply can't make trial reels)."""
    traj.log("altpath_start")
    # Path A may have left us deep (e.g. on the Professional dashboard). Reset to a
    # clean IG state so the profile + "Create New" (+) are reliably reachable; the
    # gallery video persists across this (it lives in the device gallery, not IG).
    await _open_clean(serial, traj)
    await _navigate(
        serial, traj, step="alt/profile",
        find=lambda ns: _by_text(ns, "Profile", min_y=1550),
        target=lambda ns: bool(_by_text(ns, "Create New") or _by_rid(ns, "profile_tab_layout")),
        human=False)

    nodes = await read_ui(serial)
    cn = _by_text(nodes, "Create New", exact=True) or _by_text(nodes, "Create New")
    if not cn:
        traj.deviation("alt/create_new", nodes, note="no 'Create New' (+) on profile")
        raise TrialUnavailable("no create-new button on profile")
    traj.log("step_tap", step="alt/create_new", on="Create New")
    await tap(serial, _jxy(cn), "Create New (+)", human=False)

    ok, _ = await _navigate(
        serial, traj, step="alt/create_reel",
        find=lambda ns: _by_text(ns, "Create new reel"),
        target=lambda ns: bool(_first_gallery_thumb(ns) or _by_rid(ns, "gallery_recycler_view")
                               or _by_text(ns, "Start new video")),
        tries=5, human=False)
    nodes = await read_ui(serial)
    snv = _by_text(ns := nodes, "Start new video")
    if snv:                                            # leftover draft prompt
        traj.log("step_tap", step="alt/start_new_video", on="Start new video")
        await tap(serial, _jxy(snv), "Start new video", human=False)
        await asyncio.sleep(2.0)

    ok, _ = await _navigate(
        serial, traj, step="alt/gallery", find=lambda ns: None,
        target=lambda ns: bool(_first_gallery_thumb(ns)), tries=5, human=False)
    await _select_videos_folder(serial, traj)            # Videos-only -> no stray photo
    nodes = await read_ui(serial)
    thumb = _first_gallery_thumb(nodes)
    if not thumb:
        traj.deviation("alt/pick_video", nodes, note="no gallery thumbnail (alt)")
        return _fail(traj, "alt/pick_video")
    traj.log("step_tap", step="alt/pick_video", on=_label(thumb))
    await tap(serial, _jxy(thumb), "newest video (alt)", human=False)

    ok, _ = await _navigate(
        serial, traj, step="alt/next_editor",
        find=lambda ns: (_by_rid(ns, "clips_right_action_button")
                         or _by_rid(ns, "drawer_next_button_layout")
                         or _by_text(ns, "Next", exact=True)),
        target=lambda ns: bool(_by_rid(ns, "caption_input_text_view")), tries=6, human=False)
    if not ok:
        return _fail(traj, "alt/composer")

    landed, capinfo = await _enter_caption(serial, caption, traj)
    traj.log("alt_caption", landed=landed, info=capinfo)
    if not landed:
        traj.deviation("alt/caption", await read_ui(serial), note=f"caption did not land: {capinfo}")
        return _fail(traj, "alt/caption", detail=capinfo)
    await _hide_ime(serial)

    trial_on = await _enable_trial_toggle(serial, traj)
    traj.log("alt_trial_toggle", on=trial_on)
    if not trial_on:
        traj.deviation("alt/trial_toggle", await read_ui(serial),
                       note="composer has no Trial toggle -> trial reels not enabled")
        raise TrialUnavailable("composer has no Trial toggle")

    if no_share:
        traj.log("alt_dry_run_stop")
        print("  [dry-run] alt path: caption + Trial toggle on; STOPPING before Share")
        return {"ok": True, "stage": "alt-dry-run", "detail": "trial enabled; stopped before Share",
                "traj": traj.dir, "deviations": traj.deviations}

    await _hide_ime(serial)
    await _alt_share(serial, traj)
    nodes = await read_ui(serial)
    published = bool(_by_rid(nodes, "trials_list") or _by_text(nodes, "Your reel was shared")
                     or not (_by_rid(nodes, "share_button") or _by_rid(nodes, "caption_input_text_view")
                             or _by_rid(nodes, "clips_nux_sheet_share_button")))
    traj.log("alt_publish_result", ok=published, screen=detect_screen(nodes))
    if not published:
        traj.deviation("alt/share", nodes, note="alt Share did not register")
        return _fail(traj, "alt/share")
    return {"ok": True, "stage": "alt-share", "detail": "trial reel shared via create-reel path",
            "traj": traj.dir, "deviations": traj.deviations}


# ---------------------------------------------------------------------------
# PUBLISH (Path A, instrumented)
# ---------------------------------------------------------------------------
async def publish(serial: str, caption: str, *, no_share: bool = False, traj: "Traj | None" = None) -> dict:
    traj = traj or Traj(serial, tag="publish")
    traj.log("publish_start", caption_len=len(caption), humanize=HUMANIZE, no_share=no_share)
    await _open_clean(serial, traj)
    await _ensure_adb_keyboard(serial, traj)  # overlay-less IME so Share isn't covered

    # Path A: Profile -> Professional dashboard -> 'Trial reels' -> Create trial reel.
    # If ANY step to the trial-reels list fails, the dashboard route isn't available
    # on this account -> fall back to the create-reel + Trial-toggle path (which also
    # decides, via the toggle's presence, whether trial reels are enabled at all).
    # Reach the trials list via the dashboard 'Trial reels' row, and if that route
    # isn't available, via the Best-practices path (Best practices -> Trial reels ->
    # 'Go to trial reels'). Only if BOTH dashboard paths fail do we fall back to the
    # create-reel + Trial-toggle path (which also decides eligibility via the toggle).
    if not await _reach_trials_list_any(serial, traj):
        traj.log("pathA_unavailable", at="all_dashboard_paths")
        return await _publish_via_create_reel(serial, caption, traj, no_share=no_share)

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

    # filter to Videos (Path A's "Create trial reel" gallery is mixed images+videos, so
    # a stray screenshot can sort first), then pick the newest video tile.
    await _select_videos_folder(serial, traj)
    nodes = await read_ui(serial)
    thumb = _first_gallery_thumb(nodes)
    if not thumb:
        traj.deviation("pick_video", nodes, note="no gallery thumbnail")
        return _fail(traj, "pick_video")
    traj.log("step_tap", step="pick_video", xy=list(_center(thumb)), on=_label(thumb))
    await tap(serial, _jxy(thumb), "newest video")

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

    # caption (real text; per-char humanized typing, one-shot fallback for safety)
    landed, capinfo = await _enter_caption(serial, caption, traj)
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
        await tap(serial, _jxy(sb), "Share")

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
def _caption_key(text: str) -> str:
    """Normalise a caption to lowercase alphanumeric words so the comparison
    survives curly apostrophes, emoji and UI truncation ('… more') rendering
    differences between the posted text and the on-reel a11y text."""
    t = (text or "").lower()
    return " ".join("".join(c if (c.isalnum() or c == " ") else " " for c in t).split())


def _caption_matches(reel_cap: str, expect: str):
    """True/False if the on-reel caption is clearly the same/different post as the
    one we posted; None when there isn't enough text to judge (don't block on it).
    Compares a normalised prefix so a truncated on-reel caption still matches."""
    a, b = _caption_key(reel_cap), _caption_key(expect)
    if not a or not b:
        return None
    n = min(len(a), len(b), 30)
    if n < 12:                       # too short to be a reliable fingerprint
        return None
    return a[:n] == b[:n]


def _reel_caption(nodes) -> str:
    """The caption text shown in the reel player (the real text lives in a child
    of clips_caption_component, not on the component node itself)."""
    comp = _by_rid(nodes, "clips_caption_component")
    if not comp:
        return ""
    cb = parse_bounds(comp.get("bounds"))
    if not cb:
        return ""
    best = ""
    for n in nodes:
        b = parse_bounds(n.get("bounds"))
        if not b:
            continue
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        if not (cb[0] - 5 <= cx <= cb[2] + 5 and cb[1] - 5 <= cy <= cb[3] + 5):
            continue
        t = node_text(n)
        if t and t != "clips_caption_component" and len(t) > len(best):
            best = t
    return best


async def _copy_link_once(serial, traj) -> tuple[str | None, str]:
    """Open the top-left ('newest') trial tile and copy its reel link, once.
    Returns (url, reel_caption) so the caller can confirm it's our just-posted
    reel by caption."""
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
        return None, ""
    tb = parse_bounds(tl["bounds"])
    col_w = (tb[2] - tb[0]) // 3
    await tap(serial, _jxy((tb[0] + col_w // 2, tb[1] + col_w // 2)), "newest trial tile", human=False)
    await asyncio.sleep(1.2)

    # read the caption in the reel player BEFORE opening the share sheet
    nodes = await read_ui(serial)
    caption = _reel_caption(nodes)

    opened = False
    for _ in range(4):
        if _by_text(nodes, "Copy link") or _by_rid(nodes, "search_edit_text"):
            opened = True
            break
        sb = _by_rid(nodes, "direct_share_button") or _by_rid(nodes, "share_button")
        if not sb:
            await asyncio.sleep(0.6)
            nodes = await read_ui(serial)
            continue
        await tap(serial, _jxy(sb), "Share (tile)", human=False)
        nodes = await read_ui(serial)
    if not opened:
        traj.deviation("capture/share_sheet", await read_ui(serial), note="share sheet did not open")
        return None, caption

    nodes = await read_ui(serial)
    await tap(serial, _jxy(_by_text(nodes, "Copy link")), "Copy link", human=False)
    nodes = await read_ui(serial)
    await tap(serial, _jxy(_by_rid(nodes, "search_edit_text")), "search box (focus)", human=False)
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
    return url, caption


async def _copy_link_from_trials_list(serial, traj, reject=None, expect=None) -> str | None:
    """Copy the newest trial reel's link, but only ACCEPT it when it is genuinely
    our just-posted reel: the link must be new (not in ``reject``) and, when
    ``expect`` (the caption we posted) is given, the reel's on-screen caption must
    match it. A stale/foreign reel (new post not surfaced yet) triggers a
    pull-to-refresh and retry; if it still hasn't appeared we return None so the
    caller can wait and re-navigate."""
    reject = reject or set()
    for attempt in range(3):
        url, caption = await _copy_link_once(serial, traj)
        cap_ok = _caption_matches(caption, expect) if expect else None
        is_new = bool(url and url not in reject)
        if is_new and cap_ok is not False:
            if attempt:
                traj.log("capture_new_after_refresh", url=url, attempt=attempt, cap_ok=cap_ok)
            return url
        if url and url in reject:
            traj.log("capture_reject_known", url=url, attempt=attempt,
                     note="top tile is a previously-saved reel; refreshing")
            print(f"  [capture] top tile link already saved ({url}) -> refresh & retry")
        elif cap_ok is False:
            traj.log("capture_caption_mismatch", url=url, attempt=attempt,
                     reel_cap=_caption_key(caption)[:50], note="top reel is not our post yet")
            print("  [capture] top reel caption != our post -> not appeared yet, refresh & retry")
        else:
            traj.log("capture_no_url", attempt=attempt)
        # back out of the reel/share sheet to the list, then pull-to-refresh
        await shell(serial, "input keyevent 4", timeout=10)
        await asyncio.sleep(0.6)
        await shell(serial, "input keyevent 4", timeout=10)
        await asyncio.sleep(0.6)
        nodes = await read_ui(serial)
        if not _by_rid(nodes, "trials_list"):
            await _reach_trials_list(serial, traj, human=False)
        await _swipe_down(serial)
    return None


async def capture_link(serial, traj: "Traj | None" = None, reject=None,
                       expect=None) -> tuple[str | None, str | None]:
    """Capture the newest Trial reel's public link via multiple routes.
    Returns (url, route) or (None, None). Each route logs its outcome. ``reject``
    is the set of links already saved for this account: a route that can only
    produce a rejected (stale) link is treated as a miss, so we never return a
    link that would duplicate an existing post. ``expect`` is the caption we just
    posted -- when given, the dashboard route only accepts a reel whose on-screen
    caption matches it (positively identifying our just-posted reel)."""
    traj = traj or Traj(serial, tag="capture")
    reject = reject or set()

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
                url = await _copy_link_from_trials_list(serial, traj, reject=reject, expect=expect)
                if url and url not in reject:
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
        if url and url not in reject:
            traj.log("capture_route_ok", route="reels_tab", url=url)
            return url, "reels_tab"
        if url and url in reject:
            traj.log("capture_route_reject", route="reels_tab", url=url,
                     note="reels-tab returned a previously-saved reel")
        else:
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
    try:
        res = await publish(serial, caption, no_share=no_share)
    except HardStop as e:
        print(f"\n[HARD STOP] {serial}: {e.reason} ({e.marker}) — run stopped for this device")
        return 4
    print("\nRESULT:", res)
    if not res.get("ok"):
        await _dump_screen(serial)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
