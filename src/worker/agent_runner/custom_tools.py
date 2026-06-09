"""Wire the FFFBT custom Instagram tools into the MobileRun agent.

The MobileRun ``MobileAgent`` only ships generic primitives (``click``,
``type``, ``system_button`` …). The Trial-Reel goal + AppCard, however, instruct
the agent to call project helpers like ``hide_ime`` and ``tap_share_and_confirm``.
Without these registered the agent hits *"Unknown tool"* and falls back to raw
clicks — which the Mobilerun Keyboard silently swallows on the Share screen,
producing a false ``share_did_not_register``.

This module adapts the helpers in :mod:`src.worker.tools.instagram` into the
``{name: {"function", "parameters", "description"}}`` shape MobileRun's
``ToolRegistry.register_from_dict`` consumes. Each adapter:

* is ``async`` (the registry awaits coroutine tools),
* takes ``ctx`` (MobileRun ``ActionContext``, ignored — the serial is bound at
  build time) plus any agent-supplied args,
* returns a ``(success, summary)`` tuple, which the registry normalises into its
  own ``ActionResult``.

``serial``/``video_id``/``caption`` are bound here so the agent never has to pass
device-specific values. UI snapshots required by the tap helpers are read on
demand through a lazily-connected :class:`MobilerunWorker`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.worker.tools._adb import shell as _adb_shell
from src.worker.tools._types import ToolResult
from src.worker.tools._ui import walk_plain_ui
from src.worker.tools.instagram import (
    dismiss_keyboard,
    tap_by_resource_id,
    tap_by_text,
    tap_share_and_confirm,
    verify_caption_text,
)

logger = logging.getLogger("mobilerun")

_PORTAL_STATE_URI = "content://com.mobilerun.portal/state"


def _as_tuple(result: Any) -> tuple[bool, str]:
    """Normalise a project ``ToolResult`` (or anything) into ``(success, summary)``."""
    if isinstance(result, ToolResult):
        return bool(result.success), result.message
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return True, str(result) if result else "Done"


def _parse_portal_state(raw: str) -> list[dict[str, Any]]:
    """Parse ``content query content://com.mobilerun.portal/state`` output into
    flat UI nodes. The provider returns ``Row: 0 result={...}`` where the outer
    JSON's ``result`` is itself a JSON string holding ``{"a11y_tree": [...]}``."""
    idx = raw.find("result=")
    if idx == -1:
        return []
    blob = raw[idx + len("result=") :].strip()
    try:
        outer, _ = json.JSONDecoder().raw_decode(blob)
    except Exception:
        return []
    if isinstance(outer, dict) and outer.get("status") != "success":
        return []
    inner = outer.get("result") if isinstance(outer, dict) else None
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except Exception:
            return []
    if not isinstance(inner, dict):
        return []
    return walk_plain_ui(inner.get("a11y_tree") or [])


def build_instagram_custom_tools(
    *,
    serial: str,
    video_id: str | None = None,
    caption: str | None = None,
    genfarmer_url: str | None = None,  # accepted for compat; unused
) -> dict[str, Any]:
    """Return a MobileRun ``custom_tools`` dict for the Trial-Reel flow.

    The serial (and, where relevant, the expected caption / video id) are bound
    so the agent calls the tools with no device-specific arguments.
    """

    async def _read_ui() -> list[dict[str, Any]]:
        # Read the live a11y tree straight from the on-device portal content
        # provider (reliable, ADB-only). A standalone MobilerunWorker.page_source
        # returns empty outside the agent's GenFarmer runtime.
        try:
            raw = await _adb_shell(
                serial, f"content query --uri {_PORTAL_STATE_URI}", timeout=15
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("custom_tools _read_ui shell failed: %s", exc)
            return []
        return _parse_portal_state(raw)

    # -- adapters ------------------------------------------------------------

    async def _hide_ime(ctx: Any = None, **_: Any) -> tuple[bool, str]:
        # Robust dismissal: KEYCODE_BACK alone does not hide the Mobilerun
        # Keyboard; dismiss_keyboard also clears caption focus by tapping a
        # non-input area, which reliably collapses the keyboard.
        return _as_tuple(await dismiss_keyboard(serial, _read_ui))

    async def _tap_share_and_confirm(ctx: Any = None, **_: Any) -> tuple[bool, str]:
        return _as_tuple(await tap_share_and_confirm(serial, read_ui=_read_ui))

    async def _verify_caption_text(
        ctx: Any = None, *, expected_text: str | None = None, **_: Any
    ) -> tuple[bool, str]:
        expected = expected_text or caption or ""
        if not expected:
            return False, "verify_caption_text: no expected caption bound"
        return _as_tuple(verify_caption_text(expected, ui_nodes=await _read_ui()))

    async def _tap_by_resource_id(
        ctx: Any = None, *, resource_id: str | None = None, **_: Any
    ) -> tuple[bool, str]:
        if not resource_id:
            return False, "tap_by_resource_id: resource_id is required"
        return _as_tuple(
            await tap_by_resource_id(serial, resource_id, ui_nodes=await _read_ui())
        )

    async def _tap_by_text(
        ctx: Any = None, *, text: str | None = None, **_: Any
    ) -> tuple[bool, str]:
        if not text:
            return False, "tap_by_text: text is required"
        return _as_tuple(await tap_by_text(serial, text, ui_nodes=await _read_ui()))

    return {
        "hide_ime": {
            "function": _hide_ime,
            "parameters": {},
            "description": (
                "Hide the on-screen keyboard (IME) so it stops covering bottom "
                "buttons like Share. ALWAYS call this immediately before tapping "
                "Share on the Trial Reel screen — the Mobilerun Keyboard otherwise "
                "covers the Share button and silently swallows the tap."
            ),
        },
        "tap_share_and_confirm": {
            "function": _tap_share_and_confirm,
            "parameters": {},
            "description": (
                "Publish the Trial Reel: hides the IME, taps the bottom Share "
                "button via resource-id with a real-finger swipe, then confirms "
                "the post registered (Share button gone / activity changed). This "
                "is the COMPLETE publish action — do not tap any top-right 'OK' "
                "afterwards. Returns success only when the post registered."
            ),
        },
        "verify_caption_text": {
            "function": _verify_caption_text,
            "parameters": {
                "expected_text": {
                    "type": "string",
                    "description": "Optional expected caption; defaults to the bound caption.",
                }
            },
            "description": (
                "Pre-share safety check: verify the caption field contains the "
                "intended caption. Call once before Share. Defaults to the bound caption."
            ),
        },
        "tap_by_resource_id": {
            "function": _tap_by_resource_id,
            "parameters": {
                "resource_id": {
                    "type": "string",
                    "description": "Android resource-id (full or suffix) of the element to tap.",
                }
            },
            "description": "Tap the element matching the given Android resource-id.",
        },
        "tap_by_text": {
            "function": _tap_by_text,
            "parameters": {
                "text": {
                    "type": "string",
                    "description": "Visible text of the element to tap.",
                }
            },
            "description": "Tap the smallest element whose visible text matches.",
        },
    }
