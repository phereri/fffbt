"""Instagram-specific worker tools.

Ported from Mobilerun farm/tools.py. Each tool retains the original
algorithm but accepts device_serial + ui_nodes instead of ActionContext.
"""

from __future__ import annotations

import asyncio
import base64
import shlex
import time
from typing import Any, Awaitable, Callable

from src.worker.tools._adb import (
    ime_input_shown,
    input_tap,
    shell,
    top_activity,
)
from src.worker.tools._types import ToolResult
from src.worker.tools._ui import (
    is_instagram_caption_placeholder,
    node_resource_id,
    node_text,
    normalize_caption_text,
    parse_bounds,
)

ReadUi = Callable[[], Awaitable[list[dict[str, Any]]]]


# ---------------------------------------------------------------------------
# IME
# ---------------------------------------------------------------------------


async def hide_ime(serial: str, *, timeout_s: float = 3.0) -> ToolResult:
    """Force-hide the on-screen IME so it stops covering bottom buttons.

    Strategy: check dumpsys, send KEYCODE_BACK, re-check until hidden.
    """
    if not await ime_input_shown(serial):
        return ToolResult.ok("hide_ime: IME already hidden")
    try:
        await shell(serial, "input keyevent 4", timeout=10)
    except Exception as e:
        return ToolResult.fail(f"hide_ime: keyevent BACK failed: {e}")
    deadline = time.perf_counter() + max(0.5, timeout_s)
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.25)
        if not await ime_input_shown(serial):
            return ToolResult.ok("hide_ime: IME hidden via KEYCODE_BACK")
    return ToolResult.fail(f"hide_ime: IME still shown after {timeout_s:.1f}s")


# Banner / non-input rows on the Trial Reel "New reel" screen. Tapping one of
# these clears caption focus, which dismisses the keyboard even when KEYCODE_BACK
# does not (the Mobilerun custom keyboard ignores BACK).
_DISMISS_BANNER_NEEDLES = (
    "non-follower",
    "non follower",
    "trial reel and will",
    "only be shown",
)
_DISMISS_ROW_NEEDLES = (
    "add location",
    "tag people",
    "rename audio",
    "add topics",
)


def _clear_focus_point(nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Centre of a non-editable element whose tap clears caption focus."""
    for needles in (_DISMISS_BANNER_NEEDLES, _DISMISS_ROW_NEEDLES):
        for n in nodes:
            txt = node_text(n).lower()
            if not txt or not any(k in txt for k in needles):
                continue
            cls = str(n.get("className") or n.get("class_name") or "").lower()
            if "edit" in cls:  # never tap an editable field
                continue
            b = parse_bounds(n.get("bounds"))
            if b:
                return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
    return None


async def dismiss_keyboard(
    serial: str, read_ui: "ReadUi", *, timeout_s: float = 4.0
) -> ToolResult:
    """Robustly hide the on-screen IME, including the Mobilerun custom keyboard.

    KEYCODE_BACK alone does NOT dismiss the Mobilerun Keyboard. The reliable
    method is to clear caption focus by tapping a non-input area (the Trial Reel
    banner / a settings row). Ladder: BACK -> clear-focus tap -> BACK.
    """
    if not await ime_input_shown(serial):
        return ToolResult.ok("dismiss_keyboard: IME already hidden")

    async def _hidden() -> bool:
        return not await ime_input_shown(serial)

    # 1. Clear caption focus by tapping a non-input area (handles the Mobilerun
    #    keyboard, which ignores BACK). This is non-destructive — it never
    #    navigates away from the New reel screen.
    target = _clear_focus_point(await read_ui())
    if target is not None:
        try:
            await input_tap(serial, target[0], target[1], hold_ms=60)
        except Exception:
            pass
        await asyncio.sleep(0.6)
        if await _hidden():
            return ToolResult.ok("dismiss_keyboard: hidden via clear-focus tap")

    # 2. KEYCODE_BACK as a fallback (works for stock keyboards). Only reached
    #    while the IME is still shown, so BACK targets the IME, not navigation.
    try:
        await shell(serial, "input keyevent 4", timeout=10)
    except Exception:
        pass
    await asyncio.sleep(0.5)
    if await _hidden():
        return ToolResult.ok("dismiss_keyboard: hidden via BACK")

    return ToolResult.fail(
        f"dismiss_keyboard: IME still shown after {timeout_s:.1f}s"
    )


# ---------------------------------------------------------------------------
# tap_by_resource_id
# ---------------------------------------------------------------------------


async def tap_by_resource_id(
    serial: str,
    resource_id: str,
    *,
    ui_nodes: list[dict[str, Any]],
    contains_text: str | None = None,
    class_name_contains: str | None = None,
) -> ToolResult:
    """Tap the centre of the element matching *resource_id* via adb input tap.

    Bypasses index-based resolution which is fragile after layout shifts.
    For ``caption_input_text_view``, picks the largest AutoCompleteTextView
    by area. For ``share_button``, picks the lowest on screen.
    """
    if not ui_nodes:
        return ToolResult.fail("tap_by_resource_id: empty UI tree")

    suffix = resource_id.split("/")[-1] if "/" in resource_id else resource_id
    suffix = suffix.split(":")[-1]
    needle = (contains_text or "").strip().lower()
    class_needle = (class_name_contains or "").strip().lower()

    matches: list[dict[str, Any]] = []
    for node in ui_nodes:
        rid = node_resource_id(node)
        if not rid:
            continue
        if rid != resource_id and not rid.endswith(suffix):
            continue
        if needle and needle not in node_text(node).lower():
            continue
        if not parse_bounds(node.get("bounds")):
            continue
        matches.append(node)

    if class_needle:
        filtered = [
            n
            for n in matches
            if class_needle
            in str(n.get("className") or n.get("class_name") or "").lower()
        ]
        if filtered:
            matches = filtered

    if not matches:
        return ToolResult.fail(
            f"tap_by_resource_id: no node with resource_id={resource_id!r}"
            + (f" text~{contains_text!r}" if contains_text else "")
            + (f" class~{class_name_contains!r}" if class_name_contains else "")
        )

    is_share_caption = suffix == "caption_input_text_view" or resource_id.endswith(
        "caption_input_text_view"
    )
    if is_share_caption:
        ac_only = [
            n
            for n in matches
            if "AutoCompleteTextView"
            in str(n.get("className") or n.get("class_name") or "")
        ]
        pool = ac_only if ac_only else matches

        def _area(n: dict[str, Any]) -> int:
            b = parse_bounds(n.get("bounds")) or (0, 0, 0, 0)
            return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

        target = max(pool, key=_area)
    elif suffix == "share_button" or resource_id.endswith("share_button"):
        target = max(
            matches,
            key=lambda n: (parse_bounds(n.get("bounds")) or (0, 0, 0, 0))[3],
        )
    else:
        target = max(
            matches,
            key=lambda n: (
                (parse_bounds(n.get("bounds")) or (0, 0, 0, 0))[2]
                - (parse_bounds(n.get("bounds")) or (0, 0, 0, 0))[0]
            ),
        )

    bounds = parse_bounds(target.get("bounds")) or (0, 0, 0, 0)
    x = (bounds[0] + bounds[2]) // 2
    y = (bounds[1] + bounds[3]) // 2
    try:
        await input_tap(serial, x, y)
    except Exception as e:
        return ToolResult.fail(f"tap_by_resource_id: {e}")
    return ToolResult.ok(
        f"tapped {resource_id} at ({x},{y}) [text={node_text(target)[:40]!r}]"
    )


# ---------------------------------------------------------------------------
# tap_by_text
# ---------------------------------------------------------------------------


async def tap_by_text(
    serial: str,
    text: str,
    *,
    ui_nodes: list[dict[str, Any]],
    exact: bool = False,
    prefer: str = "smallest",
    exclude_text_exact: tuple[str, ...] = (),
) -> ToolResult:
    """Tap the centre of an element whose visible text matches *text*.

    ``prefer='smallest'`` for buttons, ``'largest'`` for caption hints.
    """
    if not text:
        return ToolResult.fail("tap_by_text: empty text")
    if prefer not in ("smallest", "largest"):
        prefer = "smallest"

    needle = text.strip().lower()
    excludes = {x.strip().lower() for x in exclude_text_exact if x.strip()}
    matches: list[dict[str, Any]] = []
    for node in ui_nodes:
        nt = node_text(node).strip()
        if not nt:
            continue
        if nt.lower() in excludes:
            continue
        if not parse_bounds(node.get("bounds")):
            continue
        if exact:
            if nt.lower() == needle:
                matches.append(node)
        elif needle in nt.lower():
            matches.append(node)

    if not matches:
        return ToolResult.fail(f"tap_by_text: no node matches text~{text!r}")

    def _area(n: dict[str, Any]) -> int:
        b = parse_bounds(n.get("bounds")) or (0, 0, 0, 0)
        return max(0, (b[2] - b[0])) * max(0, (b[3] - b[1]))

    target = max(matches, key=_area) if prefer == "largest" else min(matches, key=_area)
    bounds = parse_bounds(target.get("bounds")) or (0, 0, 0, 0)
    x = (bounds[0] + bounds[2]) // 2
    y = (bounds[1] + bounds[3]) // 2
    try:
        await input_tap(serial, x, y)
    except Exception as e:
        return ToolResult.fail(f"tap_by_text: {e}")
    return ToolResult.ok(
        f"tapped text~{text!r} at ({x},{y}) "
        f"[resource_id={node_resource_id(target)!r}]"
    )


# ---------------------------------------------------------------------------
# verify_caption_text
# ---------------------------------------------------------------------------


def verify_caption_text(
    expected_text: str,
    *,
    ui_nodes: list[dict[str, Any]],
) -> ToolResult:
    """Pre-share safety check: verify the caption field matches expected text."""
    expected = normalize_caption_text(expected_text)

    candidates: list[dict[str, Any]] = []
    for node in ui_nodes:
        rid = node_resource_id(node)
        class_name = str(node.get("className") or node.get("class_name") or "")
        text = str(node.get("text") or "")
        if rid.endswith("caption_input_text_view"):
            candidates.append(node)
            continue
        if "AutoCompleteTextView" in class_name and (
            "caption" in text.lower() or "#" in text.lower()
        ):
            candidates.append(node)

    if not candidates:
        return ToolResult.fail(
            "caption verification: caption input not found in current UI"
        )

    def _score(node: dict[str, Any]) -> tuple[int, int]:
        raw = str(node.get("text") or "")
        if is_instagram_caption_placeholder(raw):
            return (0, len(raw))
        return (1, len(raw))

    candidates.sort(key=_score, reverse=True)
    observed_raw = str(candidates[0].get("text") or "")
    if is_instagram_caption_placeholder(observed_raw):
        return ToolResult.fail(
            "caption verification: field still shows Instagram placeholder "
            "(paste/focus did not apply - retry paste_text or tap the caption box)"
        )
    observed = normalize_caption_text(observed_raw)
    if observed == expected:
        return ToolResult.ok(f"caption verified exactly ({len(observed)} chars)")

    expected_flat = " ".join(expected.split())
    observed_flat = " ".join(observed.split())
    if observed_flat == expected_flat:
        return ToolResult.ok(
            f"caption verified (whitespace-tolerant, {len(observed)} chars)"
        )

    return ToolResult.fail(
        "caption verification mismatch: "
        f"expected={expected[:240]!r}; observed={observed[:240]!r}"
    )


# ---------------------------------------------------------------------------
# paste_text
# ---------------------------------------------------------------------------

_ADB_KEYBOARD_COMPONENT = "com.android.adbkeyboard/.AdbIME"
_MOBILERUN_KEYBOARD_COMPONENT = "com.mobilerun.portal/.input.MobilerunKeyboardIME"


async def _keyboard_ensure_active(serial: str) -> tuple[str | None, str | None]:
    """Switch default IME to a machine-input keyboard.

    Returns ``(previous_ime, active_kind)``. MobileRun Portal's IME is preferred
    because the current VPS path already requires MobileRun TCP/Portal and the
    legacy ADB Keyboard component is not installed on all validation phones.
    """
    try:
        prev = (
            await shell(serial, "settings get secure default_input_method", timeout=5)
        ).strip()
    except Exception:
        return None, None

    prev_lower = prev.lower()
    if "mobilerun" in prev_lower:
        return None, "mobilerun"
    if "adbkeyboard" in prev_lower:
        return None, "adbkeyboard"

    for component, marker, kind in (
        (_MOBILERUN_KEYBOARD_COMPONENT, "mobilerun", "mobilerun"),
        (_ADB_KEYBOARD_COMPONENT, "adbkeyboard", "adbkeyboard"),
    ):
        try:
            await shell(serial, f"ime enable {component}", timeout=10)
            await shell(serial, f"ime set {component}", timeout=10)
            await asyncio.sleep(0.35)
            cur = (
                await shell(serial, "settings get secure default_input_method", timeout=5)
            ).strip()
            if marker in cur.lower():
                return (prev if prev else None), kind
        except Exception:
            continue

    return None, None


async def _adb_keyboard_restore(serial: str, previous: str | None) -> None:
    if not previous or not previous.strip():
        return
    try:
        await shell(serial, f"ime set {shlex.quote(previous.strip())}", timeout=10)
    except Exception:
        pass


async def _mobilerun_keyboard_input(serial: str, encoded_text: str, *, clear: bool) -> bool:
    """Input base64 text through MobileRun Portal's keyboard provider."""
    clear_str = "true" if clear else "false"
    cmd = (
        'content insert --uri "content://com.mobilerun.portal/keyboard/input" '
        f'--bind base64_text:s:"{encoded_text}" '
        f"--bind clear:b:{clear_str}"
    )
    try:
        await shell(serial, cmd, timeout=10)
    except Exception:
        return False
    return True


async def _focus_caption_field(
    serial: str,
    ui_nodes: list[dict[str, Any]],
) -> ToolResult:
    """Focus the IG Share caption field using stable resource-id strategies."""
    rid = "com.instagram.android:id/caption_input_text_view"
    for extra in (
        {"contains_text": "Write a caption", "class_name_contains": "AutoComplete"},
        {"class_name_contains": "AutoComplete"},
        {},
    ):
        result = await tap_by_resource_id(serial, rid, ui_nodes=ui_nodes, **extra)
        if result.success:
            return result
    for hint in ("Write a caption", "Add a caption"):
        result = await tap_by_text(
            serial,
            hint,
            ui_nodes=ui_nodes,
            prefer="largest",
            exclude_text_exact=("Prompt",),
        )
        if result.success:
            return result
    return ToolResult.fail(
        "focus_caption_field: resource_id + caption hints failed"
    )


async def paste_text(
    serial: str,
    text: str,
    *,
    ui_nodes: list[dict[str, Any]],
    resource_id: str | None = None,
    focus_caption: bool = False,
    clear: bool = True,
) -> ToolResult:
    """Paste full text into a field.

    Focuses the target field (by resource_id or IG caption auto-detection),
    then inputs via MobileRun Portal keyboard when available. Falls back to the
    legacy ADB_INPUT_B64 broadcast for older devices with ADB Keyboard.

    Set ``focus_caption=True`` for Instagram Trial Reel Share caption field
    (resolves via caption_input_text_view + "Write a caption" hint).
    """
    if not text:
        return ToolResult.ok("paste_text: (empty)")

    if resource_id:
        is_ig_caption = resource_id.endswith("caption_input_text_view")
        if is_ig_caption:
            result = await _focus_caption_field(serial, ui_nodes)
        else:
            result = await tap_by_resource_id(serial, resource_id, ui_nodes=ui_nodes)
        if not result.success:
            return ToolResult.fail(f"paste_text: focus failed: {result.message}")
        await asyncio.sleep(0.55 if is_ig_caption else 0.4)
    elif focus_caption:
        result = await _focus_caption_field(serial, ui_nodes)
        if not result.success:
            return ToolResult.fail(
                f"paste_text: caption focus failed: {result.message}"
            )
        await asyncio.sleep(0.55)

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    prev_ime, keyboard_kind = await _keyboard_ensure_active(serial)
    try:
        if keyboard_kind == "mobilerun":
            if await _mobilerun_keyboard_input(serial, encoded, clear=clear):
                return ToolResult.ok(f"pasted {len(text)} chars via MobileRun keyboard")

        try:
            out = await shell(
                serial,
                f'am broadcast -a ADB_INPUT_B64 --es msg "{encoded}"',
                timeout=10,
            )
            if "Broadcast completed" in (out or ""):
                return ToolResult.ok(f"pasted {len(text)} chars via ADB_INPUT_B64")
        except Exception:
            pass

        return ToolResult.fail("paste_text: MobileRun keyboard/ADB_INPUT_B64 failed")
    finally:
        await _adb_keyboard_restore(serial, prev_ime)


# ---------------------------------------------------------------------------
# tap_share_and_confirm
# ---------------------------------------------------------------------------


async def tap_share_and_confirm(
    serial: str,
    *,
    read_ui: ReadUi,
    confirm_timeout_s: float = 22.0,
) -> ToolResult:
    """Tap Instagram's Share button and confirm the post registered.

    Hides IME, taps share_button by resource-id with real-finger swipe,
    then polls for activity change or share_button disappearing.
    Retries with escalating tap strategies on failure.
    """

    def _share_button_bounds(
        nodes: list[dict[str, Any]],
    ) -> tuple[int, int] | None:
        candidates: list[tuple[int, int, int, int]] = []
        for n in nodes:
            if node_resource_id(n).endswith("share_button"):
                b = parse_bounds(n.get("bounds"))
                if b:
                    candidates.append(b)
        if not candidates:
            return None
        b = max(candidates, key=lambda bb: bb[3])
        return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2

    def _share_button_gone(nodes: list[dict[str, Any]]) -> bool:
        for n in nodes:
            if node_resource_id(n).endswith("share_button"):
                return False
        return True

    async def _tap_share_once(
        *,
        hold_ms: int = 120,
        mode: str = "swipe",
    ) -> tuple[bool, str, tuple[int, int] | None]:
        await dismiss_keyboard(serial, read_ui)
        await asyncio.sleep(0.45)
        nodes = await read_ui()
        coords = _share_button_bounds(nodes)
        if coords is None:
            return False, "share_button not in current UI tree", None
        try:
            if mode == "shell_tap":
                await shell(
                    serial, f"input tap {coords[0]} {coords[1]}", timeout=10
                )
            else:
                await input_tap(serial, coords[0], coords[1], hold_ms=hold_ms)
        except Exception as e:
            return False, f"adb tap failed: {e}", coords
        return True, f"tapped({mode})", coords

    async def _poll_confirmation(
        started_activity: str,
        started: float,
        timeout: float,
        coords: tuple[int, int] | None,
        label: str = "",
    ) -> ToolResult | None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.7)
            cur_activity = await top_activity(serial)
            if cur_activity and cur_activity != started_activity:
                elapsed = time.perf_counter() - started
                return ToolResult.ok(
                    f"share confirmed{label}: activity {started_activity} -> "
                    f"{cur_activity} in {elapsed:.1f}s (tap at {coords})"
                )
            nodes = await read_ui()
            if _share_button_gone(nodes):
                elapsed = time.perf_counter() - started
                return ToolResult.ok(
                    f"share confirmed{label}: share_button gone in {elapsed:.1f}s"
                    f" (tap at {coords})"
                )
        return None

    started_activity = await top_activity(serial)
    started = time.perf_counter()

    # First attempt
    ok, msg, coords = await _tap_share_once()
    if not ok:
        return ToolResult.fail(f"tap_share_and_confirm: {msg}")

    result = await _poll_confirmation(
        started_activity, started, confirm_timeout_s, coords
    )
    if result:
        return result

    # Retry with shell_tap
    ok2, msg2, coords2 = await _tap_share_once(hold_ms=180, mode="shell_tap")
    if not ok2:
        cur_activity = await top_activity(serial)
        if cur_activity and cur_activity != started_activity:
            elapsed = time.perf_counter() - started
            return ToolResult.ok(
                f"share confirmed (during retry): activity {started_activity} -> "
                f"{cur_activity} in {elapsed:.1f}s"
            )
        return ToolResult.fail(f"tap_share_and_confirm: retry failed: {msg2}")

    result = await _poll_confirmation(
        started_activity, started, 12.0, coords2, " on retry"
    )
    if result:
        return result

    # Third attempt with longer hold
    ok3, _, coords3 = await _tap_share_once(hold_ms=200, mode="swipe")
    if ok3:
        result = await _poll_confirmation(
            started_activity, started, 10.0, coords3, " on 3rd tap"
        )
        if result:
            return result

    # Observe-only window for slow Trial transitions
    result = await _poll_confirmation(
        started_activity, started, 16.0, coords, " (slow)"
    )
    if result:
        return result

    # Last resort: tap_by_resource_id
    last_nodes = await read_ui()
    rid_result = await tap_by_resource_id(
        serial,
        "com.instagram.android:id/share_button",
        ui_nodes=last_nodes,
    )
    if rid_result.success:
        result = await _poll_confirmation(
            started_activity, started, 14.0, None, " after resource_id tap"
        )
        if result:
            return result

    return ToolResult.fail(
        "tap_share_and_confirm: activity did not change and share_button is still "
        "on screen after swipe + shell_tap + swipe + observe + resource_id - "
        "share did not register"
    )
