"""verification step — confirm Trial Reel was posted and optionally capture URL.

Runs after mobile_ui_automation succeeds. The device is still reserved.
Post URL capture is best-effort (max 3 agent steps); failure to capture
a URL does not fail the step.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.worker.session.mobilerun_adapter import MobilerunWorker
from src.worker.session.types import (
    StepContext,
    StepName,
    StepResult,
    StepStatus,
)

logger = logging.getLogger(__name__)

_GOAL_VERIFY_POST = (
    "Check if we are on a post-publish screen. Look for any of: "
    "(1) the trials_list with trial_thumbnail_image tiles, "
    "(2) the home feed, "
    "(3) the profile tab showing recent reels. "
    "Report success=true if a recently posted Trial Reel appears to be live."
)

_GOAL_CAPTURE_URL = (
    "Try to capture the URL of the just-posted Trial Reel. "
    "Navigate: Profile tab -> Reels grid -> tap the freshest thumbnail "
    "(top-left) -> open share/overflow menu -> 'Copy link' -> read clipboard. "
    "If any step fails or is slow, stop immediately and return success=true "
    "with post_url='' (empty). Do NOT retry or re-share."
)


class VerificationStep:
    """Verify the Trial Reel is visible and optionally capture the post URL."""

    name = StepName.VERIFICATION

    def __init__(self, *, genfarmer_url: str | None = None) -> None:
        self._genfarmer_url = genfarmer_url

    async def run(
        self,
        ctx: StepContext,
        *,
        device_serial: str | None = None,
    ) -> StepResult:
        serial = device_serial or ctx.settings.get("device_serial")
        gf_url = (
            self._genfarmer_url
            or ctx.settings.get("genfarmer_url", "http://127.0.0.1:55554")
        )

        if not serial:
            return self._fail("INFRA", "no device_serial provided")

        worker = MobilerunWorker(device_serial=serial, genfarmer_url=gf_url)
        try:
            await asyncio.to_thread(worker.connect)
        except Exception as e:
            return self._fail("INFRA", f"GenFarmer connect failed: {e}")

        try:
            verified = await self._verify_post_visible(worker)
            post_url = await self._try_capture_url(worker)

            details: dict[str, Any] = {}
            if post_url:
                details["post_url"] = post_url

            if verified:
                msg = "post verified"
                if post_url:
                    msg += f", url: {post_url}"
                return StepResult(
                    step=StepName.VERIFICATION,
                    status=StepStatus.OK,
                    message=msg,
                    details=details if details else None,
                )
            return StepResult(
                step=StepName.VERIFICATION,
                status=StepStatus.NEEDS_REVIEW,
                code="verification_failed",
                message="could not confirm post visibility",
            )
        except Exception as e:
            return self._fail("UNKNOWN", f"unhandled: {e}")
        finally:
            try:
                await asyncio.to_thread(worker.disconnect)
            except Exception:
                pass

    async def _verify_post_visible(self, worker: MobilerunWorker) -> bool:
        try:
            result = await asyncio.to_thread(
                worker.run_goal, _GOAL_VERIFY_POST, timeout_seconds=30
            )
            status = str(result.get("status", "")).lower()
            return status in ("success", "completed", "ok", "done")
        except Exception:
            return False

    async def _try_capture_url(self, worker: MobilerunWorker) -> str | None:
        try:
            result = await asyncio.to_thread(
                worker.run_goal,
                _GOAL_CAPTURE_URL,
                timeout_seconds=45,
                overrides={"max_steps": 3},
            )
            output = result.get("output") or result
            if isinstance(output, dict):
                url = output.get("post_url") or output.get("url") or ""
            else:
                url = ""
            if url and "instagram.com" in str(url):
                return str(url)
        except Exception:
            pass
        return None

    def _fail(self, code: str, message: str) -> StepResult:
        return StepResult(
            step=StepName.VERIFICATION,
            status=StepStatus.FAILED,
            code=code,
            message=message,
        )
