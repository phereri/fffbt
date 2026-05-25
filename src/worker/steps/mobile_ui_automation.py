"""mobile_ui_automation step — drive Instagram Trial Reel posting flow.

Uses MobilerunWorker.run_goal() for multi-step navigation and local
Instagram tools for critical deterministic operations (caption entry,
caption verification, share confirmation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import xml.etree.ElementTree as ET
from typing import Any

from src.worker.session.mobilerun_adapter import MobilerunWorker
from src.worker.session.types import (
    StepContext,
    StepName,
    StepResult,
    StepStatus,
)
from src.worker.tools._ui import walk_plain_ui
from src.worker.tools.instagram import (
    paste_text,
    tap_share_and_confirm,
    verify_caption_text,
)

logger = logging.getLogger(__name__)

_HARD_STOP_PATTERNS: dict[str, list[str]] = {
    "action_blocked": [
        "action blocked",
        "we restrict certain activity",
        "try again later",
    ],
    "logged_out": [
        "log in to instagram",
        "create new account",
    ],
    "login_challenge": [
        "two-factor",
        "enter the code",
        "verify your identity",
        "confirm your identity",
        "security code",
    ],
    "account_suspended": [
        "account suspended",
        "account has been disabled",
        "your account has been suspended",
    ],
    "unexpected_destructive_dialog": [
        "log out of all accounts",
        "delete your account",
    ],
}

_GOAL_OPEN_INSTAGRAM = (
    "Open Instagram app (com.instagram.android). "
    "If an account-switching dialog appears, select the account "
    "matching the current session. Wait for the app to be fully loaded."
)

_GOAL_NAVIGATE_TO_SHARE = (
    "Navigate to the Trial Reel Share screen following this exact path:\n"
    "1. Go to the Profile tab.\n"
    "2. Tap 'Professional dashboard' (may say 'Professional Tools' or 'Pro dashboard').\n"
    "3. Inside the dashboard, tap the 'Trial Reels' tile (may be 'Trial reel', 'Trial', or under 'Tools to grow').\n"
    "4. Tap the create entry: 'Create', 'Try it', 'Get started', or a centred '+'.\n"
    "5. In the composer, switch to gallery tab if needed. Select the MOST RECENT video.\n"
    "6. Tap 'Next' / arrow forward through editor screens. Do NOT enter 'Edit cover'.\n"
    "Stop when you reach the Share screen (you'll see 'Write a caption' and a Share button).\n\n"
    "IMPORTANT: Do NOT use the bottom-nav '+' button. Do NOT look for a Trial toggle on the normal Share screen. "
    "Always use the Professional dashboard path."
)


def _parse_xml_ui(xml_str: str) -> list[dict[str, Any]]:
    """Parse uiautomator dump XML into flat node dicts."""
    _ATTR_MAP = {
        "resource-id": "resourceId",
        "class": "className",
        "content-desc": "contentDescription",
        "long-clickable": "longClickable",
    }
    nodes: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return nodes
    for elem in root.iter():
        node: dict[str, Any] = {}
        for key, val in elem.attrib.items():
            mapped = _ATTR_MAP.get(key, key)
            node[mapped] = val
        if node:
            nodes.append(node)
    return nodes


def _parse_page_source(source: str) -> list[dict[str, Any]]:
    """Parse page source (XML or JSON) into flat ui_nodes list."""
    if not source or not source.strip():
        return []
    stripped = source.strip()
    if stripped.startswith("<") or stripped.startswith("<?"):
        return _parse_xml_ui(stripped)
    try:
        data = json.loads(stripped)
        return walk_plain_ui(data)
    except (json.JSONDecodeError, TypeError):
        return []


def _detect_hard_stop(ui_nodes: list[dict[str, Any]]) -> tuple[str, str] | None:
    all_text = " ".join(
        str(n.get("text") or n.get("contentDescription") or "")
        for n in ui_nodes
    ).lower()
    for code, patterns in _HARD_STOP_PATTERNS.items():
        for pattern in patterns:
            if pattern in all_text:
                return code, pattern
    return None


def _goal_succeeded(result: dict[str, Any]) -> bool:
    status = str(result.get("status", "")).lower()
    return status in ("success", "completed", "ok", "done")


class MobileUIAutomationStep:
    """Drive Instagram app to publish a Trial Reel.

    Phase 1: run_goal() to open Instagram and navigate to Share screen.
    Phase 2: local tools for caption paste, verification, and share.
    """

    name = StepName.MOBILE_UI_AUTOMATION

    def __init__(self, *, genfarmer_url: str | None = None) -> None:
        self._genfarmer_url = genfarmer_url

    async def run(
        self,
        ctx: StepContext,
        *,
        device_serial: str | None = None,
        caption_text: str | None = None,
    ) -> StepResult:
        serial = device_serial or ctx.settings.get("device_serial")
        caption = caption_text or ctx.settings.get("caption_text", "")
        gf_url = (
            self._genfarmer_url
            or ctx.settings.get("genfarmer_url", "http://127.0.0.1:55554")
        )

        if not serial:
            return self._fail("INFRA", "no device_serial provided")
        if not caption:
            return self._fail("INFRA", "no caption_text provided")

        worker = MobilerunWorker(device_serial=serial, genfarmer_url=gf_url)
        try:
            await asyncio.to_thread(worker.connect)
        except Exception as e:
            return self._fail("INFRA", f"GenFarmer connect failed: {e}")

        try:
            return await self._execute(worker, serial, caption)
        except Exception as e:
            await self._screenshot(worker, "on_error")
            return self._fail("UNKNOWN", f"unhandled: {e}")
        finally:
            try:
                await asyncio.to_thread(worker.disconnect)
            except Exception:
                pass

    async def _execute(
        self,
        worker: MobilerunWorker,
        serial: str,
        caption: str,
    ) -> StepResult:
        # --- Open Instagram ---
        result = await self._run_goal(worker, _GOAL_OPEN_INSTAGRAM, timeout=60)
        if not _goal_succeeded(result):
            ui = await self._read_ui(worker)
            stop = _detect_hard_stop(ui)
            if stop:
                return self._fail(stop[0], f"hard stop: {stop[1]}")
            return self._fail(
                "INFRA",
                f"open_instagram failed: {result.get('error', result.get('status', 'unknown'))}",
                retryable=True,
            )

        await self._screenshot(worker, "after_instagram_launch")

        ui = await self._read_ui(worker)
        stop = _detect_hard_stop(ui)
        if stop:
            return self._fail(stop[0], f"hard stop after launch: {stop[1]}")

        # --- Navigate to Share screen ---
        result = await self._run_goal(worker, _GOAL_NAVIGATE_TO_SHARE, timeout=180)
        if not _goal_succeeded(result):
            ui = await self._read_ui(worker)
            stop = _detect_hard_stop(ui)
            if stop:
                return self._fail(stop[0], f"hard stop: {stop[1]}")
            err = (
                result.get("error")
                or result.get("failure_reason")
                or result.get("status", "unknown")
            )
            err_lower = str(err).lower()
            if "dashboard" in err_lower or "trial" in err_lower:
                return self._fail("trial_reels_unavailable", str(err))
            return self._needs_review("unknown_screen", f"navigation failed: {err}")

        await self._screenshot(worker, "share_screen")

        ui = await self._read_ui(worker)
        stop = _detect_hard_stop(ui)
        if stop:
            return self._fail(stop[0], f"hard stop on Share screen: {stop[1]}")

        # --- Fill caption (local tools) ---
        paste_result = await paste_text(serial, caption, ui_nodes=ui, focus_caption=True)
        if not paste_result.success:
            return self._needs_review(
                "unknown_screen", f"caption paste failed: {paste_result.message}"
            )

        await asyncio.sleep(0.8)

        # --- Verify caption ---
        ui = await self._read_ui(worker)
        verify_result = verify_caption_text(caption, ui_nodes=ui)
        if not verify_result.success:
            return self._needs_review(
                "caption_mismatch", f"caption verification failed: {verify_result.message}"
            )

        await self._screenshot(worker, "caption_filled")

        # --- Share ---
        async def read_ui() -> list[dict[str, Any]]:
            return await self._read_ui(worker)

        share_result = await tap_share_and_confirm(serial, read_ui=read_ui)
        if not share_result.success:
            ui = await self._read_ui(worker)
            stop = _detect_hard_stop(ui)
            if stop:
                return self._fail(stop[0], f"hard stop during share: {stop[1]}")
            return self._needs_review(
                "share_did_not_register", f"share failed: {share_result.message}"
            )

        await self._screenshot(worker, "post_result")

        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.OK,
            message=f"Trial Reel published: {share_result.message}",
        )

    async def _run_goal(
        self, worker: MobilerunWorker, goal: str, timeout: int = 300
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                worker.run_goal, goal, timeout_seconds=timeout
            )
        except Exception as e:
            logger.error("run_goal error: %s", e)
            return {"status": "error", "error": str(e)}

    async def _read_ui(self, worker: MobilerunWorker) -> list[dict[str, Any]]:
        try:
            source = await asyncio.to_thread(worker.page_source)
            return _parse_page_source(source)
        except Exception:
            return []

    async def _screenshot(self, worker: MobilerunWorker, label: str) -> None:
        try:
            await asyncio.to_thread(worker.screenshot, label)
        except Exception:
            pass

    def _fail(
        self, code: str, message: str, *, retryable: bool | None = None
    ) -> StepResult:
        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.FAILED,
            code=code,
            message=message,
            retryable=retryable,
        )

    def _needs_review(self, code: str, message: str) -> StepResult:
        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.NEEDS_REVIEW,
            code=code,
            message=message,
        )
