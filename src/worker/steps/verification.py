"""verification step — two-level post-publish verification.

Level 1 (immediate): confirm the publish screen transitioned away
(trials_list visible, activity changed, or share_button gone).

Level 2 (delayed): wait verification_delay_seconds, then navigate to
Professional Dashboard / Trial Reels and confirm the reel is visible.

The device stays reserved throughout both levels.
Post URL capture is best-effort; missing URL is not failure.
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

_GOAL_VERIFY_IMMEDIATE = (
    "Check if we are on a post-publish screen. Look for any of: "
    "(1) the trials_list with trial_thumbnail_image tiles, "
    "(2) the home feed, "
    "(3) the profile tab showing recent reels. "
    "Report success=true if a recently posted Trial Reel appears to be live."
)

_GOAL_VERIFY_DASHBOARD = (
    "Verify the just-published Trial Reel is live. Trial Reels do NOT appear on "
    "the main profile grid — only in the Professional dashboard's Trial reels "
    "list. Navigate there and CONFIRM the reel before reporting anything.\n"
    "1. Go to the Profile tab.\n"
    "2. Tap the 'Professional dashboard' banner (may say 'Professional Tools' / "
    "'Pro dashboard'); it opens a screen whose title is 'Professional dashboard'.\n"
    "3. On the dashboard, the 'Trial reels' entry is a ROW near the BOTTOM of the "
    "screen (content-desc/text 'Trial reels'). Scroll down if it is not visible, "
    "then tap it. Resolve it by text/content-desc — do not guess coordinates and "
    "do not idle on the dashboard.\n"
    "4. On the Trial reels list, pull down to refresh once (swipe down from the "
    "top) — a just-posted reel often does not appear until the list is refreshed.\n"
    "5. Inspect the NEWEST tile (top-left) and confirm a freshly posted Trial "
    "Reel is present at the top of the list.\n"
    "Anti-stall: never issue repeated 'wait' actions. After at most ONE short "
    "wait for a screen to settle, take a real tap toward the goal. If two "
    "consecutive actions leave you on the same screen, tap the specific next "
    "target ('Trial reels'), or press Back to the profile and retry — do not "
    "loop on 'wait'.\n"
    "Report success=true ONLY if you actually reached the Trial reels list and "
    "can see a fresh Trial Reel at the top. If you could not reach or could not "
    "confirm the list, report success=false — never assume success."
)

_GOAL_CAPTURE_URL = (
    "Try to capture the URL of the just-posted Trial Reel. "
    "Navigate: Profile tab -> Reels grid -> tap the freshest thumbnail "
    "(top-left) -> open share/overflow menu -> 'Copy link' -> read clipboard. "
    "If any step fails or is slow, stop immediately and return success=true "
    "with post_url='' (empty). Do NOT retry or re-share."
)

_DEFAULT_VERIFICATION_DELAY = 180


class VerificationStep:
    """Two-level verification: immediate confirmation then delayed dashboard check."""

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
            # Level 1: immediate, best-effort signal only. It must NEVER
            # short-circuit the step: the publish has already completed by the
            # time verification runs, and a just-posted Trial Reel needs ~1-2
            # min to become queryable. The authoritative check is the delayed
            # Level 2 dashboard pass below. (Previously a failed Level 1 returned
            # verification_failed in ~13s, skipping the wait + dashboard check
            # and falsely failing reels that were in fact live.)
            level1 = await self._verify_immediate(worker)
            await self._screenshot(worker, "level1_verification")
            if not level1:
                logger.info(
                    "level 1 immediate check inconclusive; proceeding to "
                    "delayed dashboard verification anyway"
                )

            # Wait configured delay before Level 2 (always — the Trial Reel
            # needs time to appear in the dashboard list).
            delay = int(
                ctx.settings.get(
                    "verification_delay_seconds",
                    str(_DEFAULT_VERIFICATION_DELAY),
                )
            )
            logger.info(
                "waiting %ds before dashboard verification (level1=%s)",
                delay,
                level1,
            )
            await asyncio.sleep(delay)

            # Level 2: dashboard verification (authoritative). Runs through the
            # in-process MobileAgent (the working executor path), not
            # worker.run_goal / GenFarmer /automation/run.
            level2 = await self._verify_dashboard(serial, ctx)
            await self._screenshot(worker, "verification_result")

            post_url = await self._try_capture_url(worker)

            details: dict[str, Any] = {}
            if post_url:
                details["post_url"] = post_url

            if level2:
                msg = "post verified via dashboard"
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
                message=(
                    "level 2: could not confirm post in "
                    "Professional Dashboard"
                ),
            )
        except Exception as e:
            return self._fail("UNKNOWN", f"unhandled: {e}")
        finally:
            try:
                await asyncio.to_thread(worker.disconnect)
            except Exception:
                pass

    async def _verify_immediate(self, worker: MobilerunWorker) -> bool:
        try:
            result = await asyncio.to_thread(
                worker.run_goal, _GOAL_VERIFY_IMMEDIATE, timeout_seconds=30
            )
            status = str(result.get("status", "")).lower()
            return status in ("success", "completed", "ok", "done")
        except Exception:
            return False

    async def _verify_dashboard(self, serial: str, ctx: StepContext) -> bool:
        # Authoritative Level-2 check. Drive the dashboard-verification goal
        # through the in-process MobileAgent (the path the publish executor
        # uses and that actually navigates the device). worker.run_goal hits the
        # GenFarmer /automation/run endpoint, which returns ~immediately without
        # driving the device, so it could never confirm a live reel.
        from src.worker.agent_runner.mobilerun_agent_runner import (
            run_agent_goal,
            verification_result_model,
        )

        try:
            structured = await run_agent_goal(
                device_serial=serial,
                goal=_GOAL_VERIFY_DASHBOARD,
                config_path=ctx.settings.get("mobilerun_config_path"),
                app_cards_dir=ctx.settings.get("mobilerun_app_cards_dir"),
                trajectories_dir=ctx.settings.get("mobilerun_trajectories_dir"),
                output_model=verification_result_model(),
                timeout_seconds=int(
                    ctx.settings.get("verification_dashboard_timeout_seconds", "200")
                ),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.info("level 2 dashboard agent run raised: %s", e)
            return False

        if structured is None:
            logger.info("level 2 dashboard verification: no structured output")
            return False
        if isinstance(structured, dict):
            confirmed = bool(structured.get("success"))
        else:
            confirmed = bool(getattr(structured, "success", False))
        logger.info("level 2 dashboard verification: confirmed=%s", confirmed)
        return confirmed

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

    async def _screenshot(self, worker: MobilerunWorker, label: str) -> None:
        try:
            await asyncio.to_thread(worker.screenshot, label)
        except Exception:
            pass

    def _fail(self, code: str, message: str) -> StepResult:
        return StepResult(
            step=StepName.VERIFICATION,
            status=StepStatus.FAILED,
            code=code,
            message=message,
        )
