"""``post_one`` — publish a single Trial Reel on one prepared device, no DB.

The full production pipeline reserves work from Postgres and drives many
devices through the launcher. For the emergency MVP we only need the two steps
that actually touch the phone, wired directly:

  1. ``VideoPreparationStep`` — download (local path OR http(s)/S3 URL),
     transcode for Android, ``adb push`` to the gallery. Pure ADB.
  2. ``MobileUIAutomationStep`` (mobilerun_agent executor) — drive Instagram via
     the in-process ``MobileAgent`` + Instagram AppCard to publish the Trial
     Reel.
  3. (optional) a delayed Professional-dashboard confirmation through the same
     in-process agent path (``run_agent_goal``).

No Supabase, no GenFarmer, no identity / fingerprint / proxy mutation. The
device is assumed already prepared: IG logged in, Mobilerun Portal installed and
bound. A synthetic ``StepContext`` is built because the downstream steps only
read ``settings`` / ``job_id`` / ``video_id`` — never a real DB row.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.worker.session.types import (
    Mode,
    StepContext,
    StepStatus,
)
from src.worker.steps.mobile_ui_automation import MobileUIAutomationStep
from src.worker.steps.video_preparation import VideoPreparationStep

logger = logging.getLogger(__name__)

_DEFAULT_VERIFY_DELAY_SECONDS = 180


@dataclass
class PostOneResult:
    """Outcome of a single standalone post attempt.

    ``success`` is the overall verdict the CLI exits on:
      * verify enabled  -> published AND dashboard-confirmed
      * verify disabled -> published

    ``post_url`` is best-effort: Instagram often does not expose a public Trial
    Reel link for 1-2 minutes after publishing (and sometimes not at all), so a
    null URL on a successful post is normal — the post is still live.
    """

    success: bool
    published: bool
    verified: bool | None
    message: str
    code: str | None = None
    post_url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _looks_like_url(video: str) -> bool:
    return "://" in video


async def post_one(
    *,
    device_serial: str,
    video: str,
    caption: str,
    hashtags: list[str] | None = None,
    expected_username: str | None = None,
    verify: bool = True,
    verify_delay_seconds: int = _DEFAULT_VERIFY_DELAY_SECONDS,
    capture_url: bool = True,
    bucket_video_id: str | None = None,
    category: str | None = None,
    source_key: str | None = None,
    log_path: str | None = None,
    settings: dict[str, Any] | None = None,
) -> PostOneResult:
    """Publish one Trial Reel, confirm it, capture its link, and log the result.

    Parameters mirror the CLI. ``video`` may be a local ``.mp4`` path or any
    ``http(s)``/S3 presigned URL. ``settings`` lets a caller pass through
    Mobilerun overrides (config_path, app_cards_dir, trajectories_dir, etc.);
    it is merged into the synthetic ``StepContext.settings``.

    The bucket-provenance fields (``bucket_video_id`` = the S3 folder name,
    ``category`` from its meta.json, ``source_key`` = the exact S3 key) are not
    used to drive posting — they are recorded verbatim in the posted-reels log
    so the link can later be tied back to the source video. On any published
    outcome a line is appended to the JSONL log (``log_path`` /
    ``POSTED_REELS_LOG`` / ``posted_reels.jsonl``).
    """
    if not device_serial:
        return PostOneResult(False, False, None, "no device_serial provided", "INFRA")
    if not caption:
        return PostOneResult(False, False, None, "no caption provided", "INFRA")
    if not video:
        return PostOneResult(False, False, None, "no video provided", "INFRA")

    hashtags = list(hashtags or [])
    job_id = str(uuid.uuid4())
    video_id = str(uuid.uuid4())

    ctx_settings: dict[str, Any] = {
        "device_serial": device_serial,
        "caption_base": caption,
        "hashtags": hashtags,
        "expected_username": expected_username,
        # Force the in-process agent executor (the proven publish path).
        "mobile_ui_executor": "mobilerun_agent",
    }
    if settings:
        ctx_settings.update(settings)

    ctx = StepContext(
        job_id=job_id,
        video_id=video_id,
        account_id="standalone",
        account_environment_id="standalone",
        device_id=device_serial,
        mode=Mode.PROOF_OF_POSTING,
        settings=ctx_settings,
    )

    # --- Step 1: video preparation (download/transcode/push) -----------------
    prep_kwargs: dict[str, Any] = {"device_serial": device_serial}
    if _looks_like_url(video):
        prep_kwargs["video_url"] = video
    else:
        prep_kwargs["local_video_path"] = video
        # The agent goal's skip-prep branch keys off host_video_in_gallery,
        # which video_preparation sets after a successful push; we also pass
        # local_video_path through settings as a fallback for the goal text.
        ctx.settings["local_video_path"] = video

    logger.info("post_one: preparing video for %s", device_serial)
    prep = await VideoPreparationStep().run(ctx, **prep_kwargs)
    if prep.status != StepStatus.OK:
        return PostOneResult(
            success=False,
            published=False,
            verified=None,
            message=f"video_preparation failed: {prep.message}",
            code=prep.code or "video_preparation_failed",
            details={"video_preparation": prep.message},
        )

    # --- Step 2: publish via the in-process MobileAgent ----------------------
    # caption_text folds hashtags in for any deterministic fallback; the agent
    # executor itself reads caption_base + hashtags from settings.
    caption_text = caption
    tag_str = " ".join(f"#{h.lstrip('#')}" for h in hashtags if h.strip())
    if tag_str:
        caption_text = f"{caption.rstrip()}\n\n{tag_str}".strip()

    logger.info("post_one: publishing Trial Reel on %s", device_serial)
    publish = await MobileUIAutomationStep().run(
        ctx, device_serial=device_serial, caption_text=caption_text
    )

    # Provenance recorded on every logged line (independent of outcome).
    log_ctx = dict(
        bucket_video_id=bucket_video_id,
        category=category,
        source_key=source_key,
        source_video=video,
        device=device_serial,
        account=expected_username,
        caption=caption_text,
        log_path=log_path,
    )

    if publish.status != StepStatus.OK:
        _log_post(status="failed", verified=None, post_url=None,
                  code=publish.code or "publish_failed", **log_ctx)
        return PostOneResult(
            success=False,
            published=False,
            verified=None,
            message=f"publish failed: {publish.message}",
            code=publish.code or "publish_failed",
            details={"publish": publish.message, "publish_status": publish.status.value},
        )

    # --- Step 3: delayed dashboard confirmation (in-process agent) -----------
    verified: bool | None = None
    if verify:
        verified = await _verify_dashboard(ctx, device_serial, verify_delay_seconds)

    # --- Step 4: best-effort post-URL capture --------------------------------
    post_url: str | None = None
    if capture_url:
        post_url = await _capture_post_url(ctx, device_serial)

    # --- Step 5: record the result -------------------------------------------
    if verify and not verified:
        _log_post(status="published_unverified", verified=False, post_url=post_url,
                  code="verification_failed", **log_ctx)
        return PostOneResult(
            success=False,
            published=True,
            verified=False,
            message=(
                "Trial Reel published but NOT confirmed in the Professional "
                "dashboard (needs review)"
            ),
            code="verification_failed",
            post_url=post_url,
            details={"publish": publish.message},
        )

    status = "published" if verified else "published_unverified"
    _log_post(status=status, verified=verified, post_url=post_url, code=None, **log_ctx)
    msg = "Trial Reel published"
    if verified:
        msg += " and confirmed in Professional dashboard"
    elif not verify:
        msg += " (verification skipped)"
    if post_url:
        msg += f"; url: {post_url}"
    return PostOneResult(
        success=True,
        published=True,
        verified=verified,
        message=msg,
        post_url=post_url,
        details={"publish": publish.message},
    )


def _log_post(
    *,
    status: str,
    verified: bool | None,
    post_url: str | None,
    code: str | None,
    bucket_video_id: str | None,
    category: str | None,
    source_key: str | None,
    source_video: str | None,
    device: str | None,
    account: str | None,
    caption: str | None,
    log_path: str | None,
) -> None:
    """Append one line to the posted-reels JSONL log (best-effort, never raises)."""
    try:
        from src.runner.posted_log import PostedRecord, append_record

        append_record(
            PostedRecord.now(
                status=status,
                video_id=bucket_video_id,
                category=category,
                source_key=source_key,
                source_video=source_video,
                post_url=post_url,
                device=device,
                account=account,
                caption=caption,
                verified=verified,
                code=code,
            ),
            log_path,
        )
    except Exception as e:  # pragma: no cover - logging must never break a post
        logger.warning("post_one: failed to write posted log: %s", e)


async def _capture_post_url(ctx: StepContext, device_serial: str) -> str | None:
    """Best-effort: drive the URL-capture goal through the in-process agent.

    Reuses the worker's ``_GOAL_CAPTURE_URL`` text. Returns a validated
    instagram.com URL or None — IG frequently withholds the public link for the
    first 1-2 minutes after publishing, so None is an expected, non-fatal
    outcome.
    """
    from src.worker.agent_runner.mobilerun_agent_runner import run_agent_goal
    from src.worker.steps.verification import _GOAL_CAPTURE_URL

    try:
        structured = await run_agent_goal(
            device_serial=device_serial,
            goal=_GOAL_CAPTURE_URL,
            config_path=ctx.settings.get("mobilerun_config_path"),
            app_cards_dir=ctx.settings.get("mobilerun_app_cards_dir"),
            trajectories_dir=ctx.settings.get("mobilerun_trajectories_dir"),
            output_model=_url_capture_model(),
            timeout_seconds=int(ctx.settings.get("url_capture_timeout_seconds", "60")),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.info("post_one: url capture raised: %s", e)
        return None

    if structured is None:
        return None
    if isinstance(structured, dict):
        url = structured.get("post_url") or structured.get("url") or ""
    else:
        url = getattr(structured, "post_url", "") or ""
    url = str(url).strip()
    if url and "instagram.com" in url:
        return url
    return None


def _url_capture_model() -> type:
    from pydantic import BaseModel, Field

    class UrlCaptureResult(BaseModel):
        post_url: str | None = Field(
            default=None,
            description=(
                "The public instagram.com URL of the just-posted Trial Reel, "
                "read from Copy link / clipboard. Empty if not available."
            ),
        )

    return UrlCaptureResult


async def _verify_dashboard(
    ctx: StepContext, device_serial: str, delay_seconds: int
) -> bool:
    """Wait, then run the Level-2 dashboard goal through the in-process agent.

    Reuses the hardened dashboard goal text and structured-output model from the
    worker so behaviour matches the production verification step — minus the
    GenFarmer Level-1 / URL-capture passes the standalone flow does not need.
    """
    from src.worker.agent_runner.mobilerun_agent_runner import (
        run_agent_goal,
        verification_result_model,
    )
    from src.worker.steps.verification import _GOAL_VERIFY_DASHBOARD

    logger.info("post_one: waiting %ds before dashboard verification", delay_seconds)
    await asyncio.sleep(delay_seconds)

    try:
        structured = await run_agent_goal(
            device_serial=device_serial,
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
        logger.info("post_one: dashboard verification raised: %s", e)
        return False

    if structured is None:
        logger.info("post_one: dashboard verification produced no structured output")
        return False
    if isinstance(structured, dict):
        confirmed = bool(structured.get("success"))
    else:
        confirmed = bool(getattr(structured, "success", False))
    logger.info("post_one: dashboard verification confirmed=%s", confirmed)
    return confirmed


__all__ = ["post_one", "PostOneResult"]
