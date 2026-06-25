#!/usr/bin/env python3
"""Claim one fffbt.videos row, post it as a Trial Reel, verify, write back.

MVP glue between the Supabase ``fffbt.videos`` table and the standalone
``src/runner`` posting path. It does NOT touch ``automation.*`` and only ever
drives the single device passed via ``--device``.

Lifecycle (status strings are CONSTANTS below — confirm against live data):

    new  --claim-->  posting  --published-->  verification  --ok-->  posted
                        |                          |
                        +------- on failure -------+--> back to 'new'

DB access is via the Supabase Management API (PAT) so no psycopg is needed.
The video file comes from the row's ``link_drive`` (an ``s3://`` URI); we
presign it so the runner's http downloader can fetch it. The caption comes from
the S3 batch ``meta.json`` and is uniquified per reel (kept < 100 chars).

This module is intentionally close to ``src.runner.post_one`` but re-orchestrates
the steps so it can (a) flip the DB status at the right moments and (b) time the
publish and verification phases separately.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import selectors
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- repo imports: ensure repo root is importable so `src...` resolves even
# when launched as `python scripts/post_trial.py` ---------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ -> account_identity

import account_identity as ai
from src.runner import account_memory
from src.runner import fleet_events
from src.runner.post_one import _verify_dashboard
from src.runner.s3_source import FermaS3
from src.worker.tools.instagram import capture_trial_reel_link
from src.worker.session.types import Mode, StepContext, StepStatus
from src.worker.steps.mobile_ui_automation import MobileUIAutomationStep
from src.worker.steps.video_preparation import VideoPreparationStep

logger = logging.getLogger("post_trial")

# === CONFIRM THESE AGAINST LIVE DATA =======================================
# Live fffbt.videos currently contains: new / verify / posted / cancel.
# The operator's stated flow uses: new -> posting -> verification -> posted.
# If your other agents filter on 'verify', change STATUS_VERIFYING to 'verify'.
STATUS_NEW = "new"
STATUS_CLAIMED = "posting"  # set the instant we reserve a row
STATUS_VERIFYING = "verify"  # set after publish, while confirming (matches live data)
STATUS_DONE = "posted"      # set after verification succeeds
PLATFORM = "Instagram"

# Only post videos no older than this many days (by the VID_YYYYMMDD date in the
# filename). 0 / unset disables the gate. Keeps the fleet off stale content even
# if old objects linger in the bucket / DB.
# Claim-layer age gate is OFF by default: the 7-day age limit lives ONLY at the
# S3->DB sync (src/runner/s3_sync.py, S3_SYNC_MAX_AGE_DAYS) — once a video is in
# the DB it is postable regardless of how long it has waited in the 'new' queue.
# Set POST_MAX_AGE_DAYS>0 to re-enable a claim-time age filter if ever needed.
MAX_AGE_DAYS = int(os.environ.get("POST_MAX_AGE_DAYS", "0") or "0")
CAPTION_MAX_LEN = 2200  # Instagram's hard caption limit; captions are posted in full
# ===========================================================================

# Caption uniquification: rewrite the S3 meta.json caption via an LLM so each
# reel's text is unique while preserving topic, tone, and (critically) the last
# 5 hashtags. Operator-supplied system prompt, used verbatim.
UNIQUIFY_SYSTEM_PROMPT = """\
You are an expert social media copywriter specializing in content variation and text uniqueness.

Your task is to generate multiple unique versions of a video description while preserving the original topic, intent, and meaning.

Rules:

1. Maintain the original topic, context, and overall message.
2. Preserve the emotional tone and purpose of the text.
3. Rewrite sentences using different wording, structure, and phrasing.
4. Reorder ideas naturally when appropriate.
5. Make each version appear as if written by a different person.
6. Do NOT introduce new facts, claims, events, statistics, or information that are not present in the original text.
7. Do NOT significantly change the meaning.
8. Keep the length within ±20% of the original.
9. Avoid repetitive sentence patterns across versions.
10. Preserve all names, teams, players, tournaments, brands, locations, and proper nouns exactly as written.
11. The LAST 5 hashtags in the original text MUST remain completely unchanged.
12. Do NOT modify, remove, translate, reorder, or replace those final 5 hashtags.
13. Generate natural human-like writing, not AI-sounding text.
14. Version should have approximately 70–85% textual uniqueness while maintaining the same core message.
15. Output only the rewritten description without explanations, notes, numbering, or additional commentary."""


# ---------------------------------------------------------------------------
# Supabase Management API (read + write SQL via PAT)
# ---------------------------------------------------------------------------
def _load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _mgmt_query(sql: str) -> list[dict]:
    ref = os.environ["SUPABASE_PROJECT_REF"]
    pat = os.environ["SUPABASE_PAT"]
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "User-Agent": "fffbt-post-trial/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Management API query failed ({e.code}): {detail}") from None
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected Management API response: {data!r}")
    return data


def _lit(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def claim_one(category: str, order: str = "asc", max_age_days: int | None = None) -> dict | None:
    """Atomically flip one matching 'new' row to 'posting' and return it.

    A single UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) makes the
    claim race-safe against other agents: each concurrent claim locks a
    different row, and the status pre-check guarantees exactly one winner.
    ``order`` picks oldest-first ("asc", default) or newest-first ("desc") by
    created_at. Always status = 'new'. By default there is NO age filter here — age
    is enforced only at S3->DB ingest, so anything that reached the DB is postable.
    Passing ``max_age_days`` > 0 (or POST_MAX_AGE_DAYS) re-adds a claim-time filter:
    a row is then eligible only if the VID_YYYYMMDD date in its name is within that
    many days, and undated names are skipped.
    """
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    if max_age_days is None:
        max_age_days = MAX_AGE_DAYS
    age_clause = ""
    if max_age_days and max_age_days > 0:
        # First 8-digit run in the name is the YYYYMMDD shoot date; NULL (no date)
        # fails the comparison and the row is skipped.
        age_clause = (
            f"\n              AND to_date(substring(name from '[0-9]{{8}}'), 'YYYYMMDD') "
            f">= current_date - interval '{int(max_age_days)} days'"
        )
    sql = f"""
        UPDATE fffbt.videos v
        SET status = {_lit(STATUS_CLAIMED)}, updated_at = now()
        WHERE v.id = (
            SELECT id FROM fffbt.videos
            WHERE status = {_lit(STATUS_NEW)}
              AND category = {_lit(category)}
              AND platform = {_lit(PLATFORM)}{age_clause}
            ORDER BY created_at {direction}
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING v.*;
    """
    rows = _mgmt_query(sql)
    return rows[0] if rows else None


def set_status(video_id: str, status: str, **fields: Any) -> None:
    assignments = [f"status = {_lit(status)}", "updated_at = now()"]
    for col, val in fields.items():
        if col == "published_at" and val == "now":
            assignments.append("published_at = now()")
        else:
            assignments.append(f"{col} = {_lit(val)}")
    sql = (
        f"UPDATE fffbt.videos SET {', '.join(assignments)} "
        f"WHERE id = {_lit(video_id)} RETURNING id;"
    )
    _mgmt_query(sql)


def link_exists(url: str) -> bool:
    """True if any row already carries this exact reel link. Reel URLs are
    globally unique, so a captured link that already exists means we grabbed the
    wrong (stale) tile — never write it again as a new post's link."""
    if not url:
        return False
    return bool(_mgmt_query(
        f"SELECT 1 FROM fffbt.videos WHERE link_platform = {_lit(url)} LIMIT 1;"))


def account_links(account: str) -> set[str]:
    """Every reel link already saved for one account — the capture reject set so
    a fresh capture must surface a genuinely NEW reel, not the same top tile."""
    if not account:
        return set()
    rows = _mgmt_query(
        f"SELECT link_platform FROM fffbt.videos "
        f"WHERE posted_by = {_lit(account)} AND link_platform IS NOT NULL;")
    return {r["link_platform"] for r in rows if r.get("link_platform")}


# ---------------------------------------------------------------------------
# Video source + caption
# ---------------------------------------------------------------------------
def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """'s3://bucket/key/parts' -> ('bucket', 'key/parts')."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// uri: {uri!r}")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 uri: {uri!r}")
    return bucket, key


def _batch_folder(key: str) -> str:
    """ferma/Gussi/VID_x.mp4 -> 'Gussi' (the video_id folder name)."""
    parts = key.strip("/").split("/")
    # drop the shared prefix (e.g. 'ferma') and the filename, keep the folder
    return parts[-2] if len(parts) >= 2 else parts[0]


def presign(s3: FermaS3, bucket: str, key: str, expires: int = 3600) -> str:
    return s3.client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
    )


def _last_n_hashtags(text: str, n: int = 5) -> list[str]:
    return re.findall(r"#[^\s#]+", text)[-n:]


def _llm_chat(system: str, user: str, *, model: str, base_url: str, api_key: str,
              temperature: float = 0.9, timeout: int = 60, max_tokens: int | None = None) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    msg = data["choices"][0]["message"]
    return (msg.get("content") or "").strip()


def _short_err(e: object, limit: int = 220) -> str:
    """Compact, log-safe error string. For an HTTPError, include the status code
    and the upstream error body (where ShopAIKey reports 'Invalid token' / quota)."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"HTTP {e.code}: {body}".strip()[:limit]
    return (str(e) or e.__class__.__name__)[:limit]


# Where the posting path records its last uniquify outcome so the dashboard can
# surface a banner when the primary (ShopAIKey) key is failing.
UNIQUIFY_STATUS_PATH = Path(__file__).resolve().parents[1] / "data" / "uniquify_status.json"


def _record_uniquify(ok: bool, provider: str | None, *, primary_ok: bool | None = None,
                     errors: dict | None = None) -> None:
    """Persist the last uniquify outcome (best-effort, never raises)."""
    rec = {"ts": time.time(), "ok": bool(ok), "provider": provider,
           "primary_ok": primary_ok, "errors": errors or {}}
    try:
        UNIQUIFY_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIQUIFY_STATUS_PATH.write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass


def read_uniquify_status() -> dict | None:
    """Last recorded uniquify outcome from a real post, or None."""
    try:
        return json.loads(UNIQUIFY_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _uniquify_providers() -> list[dict]:
    """Ordered uniquifier backends: primary (ShopAIKey) then Gemini fallback.
    Backends without an API key are omitted. The Gemini fallback uses the
    operator's Google AI Studio key (UNIQUIFY_FALLBACK_API_KEY / GEMINI_API_KEY /
    GOOGLE_API_KEY) against Google's OpenAI-compatible endpoint, so it works even
    when the ShopAIKey balance/token is exhausted."""
    provs: list[dict] = []
    primary_key = os.environ.get("OPENAI_API_KEY", "")
    if primary_key:
        provs.append({
            "name": "shopaikey", "api_key": primary_key,
            "base_url": os.environ.get("UNIQUIFY_BASE_URL", "https://api.shopaikey.com/v1"),
            "model": os.environ.get("UNIQUIFY_MODEL", "gemini-2.5-flash"),
        })
    fb_key = (os.environ.get("UNIQUIFY_FALLBACK_API_KEY")
              or os.environ.get("GEMINI_API_KEY")
              or os.environ.get("GOOGLE_API_KEY", ""))
    if fb_key:
        provs.append({
            "name": "gemini", "api_key": fb_key,
            "base_url": os.environ.get("UNIQUIFY_FALLBACK_BASE_URL",
                                       "https://generativelanguage.googleapis.com/v1beta/openai"),
            "model": os.environ.get("UNIQUIFY_FALLBACK_MODEL", "gemini-2.5-flash"),
        })
    return provs


def uniquify_probe(timeout: int = 12) -> dict:
    """Cheap 1-token health check of each uniquifier backend (for the dashboard).
    Never raises. Returns primary_ok / fallback_ok and which backend would serve."""
    provs = _uniquify_providers()
    res: dict = {"checked_at": time.time(), "configured": bool(provs), "providers": [],
                 "primary_ok": None, "fallback_ok": None, "effective": None}
    for prov in provs:
        ok, err = False, None
        try:
            _llm_chat("You reply with a single word.", "ping", model=prov["model"],
                      base_url=prov["base_url"], api_key=prov["api_key"],
                      temperature=0, timeout=timeout, max_tokens=1)
            ok = True
        except Exception as e:
            err = _short_err(e)
        res["providers"].append({"name": prov["name"], "ok": ok, "error": err})
        if prov["name"] == "shopaikey":
            res["primary_ok"] = ok
        if prov["name"] == "gemini":
            res["fallback_ok"] = ok
        if ok and res["effective"] is None:
            res["effective"] = prov["name"]
    return res


class UniquifyUnavailable(Exception):
    """No caption-uniquifier backend could be reached (all backends failed, or none
    is configured). Raised by ``uniquify_caption(..., raise_on_unavailable=True)`` so
    the scripted poster can STOP a device rather than post a non-unique description."""

    def __init__(self, detail: str, errors: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or {}

    @property
    def reason(self) -> str:
        """Most useful single line: the primary's error if present, else the detail."""
        return self.errors.get("shopaikey") or self.detail


def uniquify_caption(base: str, *, raise_on_unavailable: bool = False) -> str:
    """Rewrite the caption via the LLM uniquifier; preserve the last 5 hashtags.

    Falls back to the original caption (never raises) if the LLM call fails, the
    output is empty, or the final 5 hashtags were not preserved verbatim — a bad
    rewrite must not break or degrade a post.

    With ``raise_on_unavailable=True``, an inability to reach ANY uniquifier backend
    (all failed, or none configured) raises ``UniquifyUnavailable`` instead of
    returning the original caption — letting the caller STOP rather than post a
    non-unique description. Content-validation rejections still fall back to the
    original (the service is up; only this one rewrite was rejected).
    """
    base = base.strip()
    provs = _uniquify_providers()
    if not provs:
        logger.warning("uniquify: no API key (OPENAI_API_KEY / GOOGLE_API_KEY); posting original caption")
        _record_uniquify(False, None, errors={"config": "no api key"})
        if raise_on_unavailable:
            raise UniquifyUnavailable("no uniquifier API key configured", {"config": "no api key"})
        return base
    # The system prompt asks for "multiple versions"; for one reel we need one.
    user_msg = (
        "Rewrite the description below into exactly ONE unique version, following "
        "all the rules. Output only that single rewritten description.\n\n" + base
    )
    # Try each backend in order (ShopAIKey primary -> Gemini fallback). A primary
    # that is out of balance / has an invalid token fails here and we fall through.
    out = ""
    served: str | None = None
    errors: dict[str, str] = {}
    for prov in provs:
        try:
            out = _llm_chat(UNIQUIFY_SYSTEM_PROMPT, user_msg, model=prov["model"],
                            base_url=prov["base_url"], api_key=prov["api_key"])
        except Exception as e:
            errors[prov["name"]] = _short_err(e)
            logger.warning("uniquify: provider %r failed (%s); trying next",
                           prov["name"], errors[prov["name"]])
            continue
        if out:
            served = prov["name"]
            break
        errors[prov["name"]] = "empty response"
        logger.warning("uniquify: provider %r returned empty; trying next", prov["name"])
    has_primary = any(p["name"] == "shopaikey" for p in provs)
    primary_ok = ("shopaikey" not in errors) if has_primary else None
    if not out:
        logger.warning("uniquify: all providers failed (%s); posting original caption", errors)
        _record_uniquify(False, None, primary_ok=primary_ok, errors=errors)
        if raise_on_unavailable:
            raise UniquifyUnavailable("all uniquifier backends failed", errors)
        return base
    if served != "shopaikey":
        logger.warning("uniquify: primary down (%s); served via fallback %r",
                       errors.get("shopaikey", "n/a"), served)
    # A backend responded -> the uniquifier service is healthy. (Content-validation
    # rejections below return the original caption but do not mark the API unhealthy.)
    _record_uniquify(True, served, primary_ok=primary_ok, errors=errors or None)

    want = _last_n_hashtags(base)
    # Safety: if the model still emitted multiple versions, keep only the first
    # by cutting at the end of the first occurrence of the preserved footer.
    if want:
        footer = " ".join(want)
        idx = out.find(footer)
        if idx != -1:
            out = out[: idx + len(footer)].strip()
    # Rule 11/12 guardrail: the last 5 hashtags must survive verbatim.
    if want and _last_n_hashtags(out) != want:
        logger.warning("uniquify: last-5 hashtags changed; posting original caption")
        return base
    # Length sanity (rule 8 is ±20%): reject a still-bloated rewrite.
    if len(out) > max(int(len(base) * 1.4), 280):
        logger.warning("uniquify: rewrite too long (%d vs %d); posting original",
                       len(out), len(base))
        return base
    if len(out) > CAPTION_MAX_LEN:
        out = out[:CAPTION_MAX_LEN]
    return out


# ---------------------------------------------------------------------------
# Posting (mirrors post_one, with status transitions + phase timing)
# ---------------------------------------------------------------------------
@dataclass
class RunOutcome:
    success: bool
    published: bool
    verified: bool | None
    post_url: str | None
    code: str | None
    message: str
    path_used: str | None = None
    verify_route: str | None = None  # which route confirmed: 'reels' | 'dashboard'
    prep_seconds: float = 0.0
    publish_seconds: float = 0.0
    verify_seconds: float = 0.0
    total_seconds: float = 0.0


def _portal_read_ui(device: str):
    """Build a no-arg ReadUi closure that reads the device's Portal a11y tree."""
    from src.worker.agent_runner.custom_tools import _parse_portal_state
    from src.worker.tools._adb import shell as _adb_shell

    async def _read_ui():
        try:
            raw = await _adb_shell(
                device, "content query --uri content://com.mobilerun.portal/state", timeout=15
            )
            return _parse_portal_state(raw)
        except Exception:
            return []

    return _read_ui


async def _capture_url_with_retry(ctx, device, attempts, delay) -> str | None:
    """Deterministically read the just-posted reel's link (no LLM, no hallucination).

    Walks Profile -> Reels -> Drafts/Trial selector -> Trial reels -> first tile
    -> Share -> Copy link, then pastes the clipboard into a field and reads the
    real URL. IG often exposes the link only after a short delay, so retry.
    """
    read_ui = _portal_read_ui(device)
    for i in range(attempts):
        # Relaunch Instagram clean first: the capture navigates Profile -> Reels,
        # which only works reliably from a known (home-feed) state — verification
        # leaves the app deep in the Professional dashboard otherwise.
        await _open_instagram_clean(device)
        url = await capture_trial_reel_link(device, read_ui)
        if url:
            return url
        if i + 1 < attempts:
            logger.info("post_trial: url not ready, retrying in %ds (%d/%d)", delay, i + 1, attempts)
            await asyncio.sleep(delay)
    return None


def _is_reel_url(u: str | None) -> bool:
    """A syntactically valid public Instagram reel link (used as a confirmation
    signal independent of the flaky dashboard check)."""
    return bool(u) and "instagram.com/reel/" in str(u)


async def _confirm_via_dashboard(ctx, device, first_delay, verify_attempts, verify_retry_delay) -> bool:
    """LLM Professional-dashboard confirmation (a few quick attempts).

    ``first_delay`` is the settle before attempt 0; pass 0 when the caller has
    already settled (so we don't double-wait)."""
    verified = False
    for attempt in range(max(1, verify_attempts)):
        delay = first_delay if attempt == 0 else verify_retry_delay
        verified = await _verify_dashboard(ctx, device, delay)
        if verified:
            return True
        if attempt + 1 < verify_attempts:
            logger.info("post_trial: not verified yet, retry %d/%d", attempt + 1, verify_attempts)
    return verified


async def _confirm_post(
    ctx, device, *,
    verify_delay: int, verify_attempts: int, verify_retry_delay: int,
    url_attempts: int, url_retry_delay: int,
    preferred_verify_path: str | None,
) -> tuple[bool, str | None, str | None]:
    """Confirm the reel is live and grab its link, trying the learned route first.

    Two routes reach the Trial Reels list:
      * 'reels'     — deterministic capture_trial_reel_link (also yields the URL);
      * 'dashboard' — LLM Professional-dashboard check.
    Returns (confirmed, post_url, route_that_confirmed). Default order is
    reels-first (deterministic + yields the link); a learned 'dashboard' flips it.
    The other route is still used as a fallback so a single flaky route never
    fails an actually-live post.
    """
    # Reels-only by default: the deterministic capture is the reliable, fast
    # confirmation. The LLM dashboard route is slow (≫10 min once humanized
    # action delays apply) and flaky, so it is OFF unless explicitly re-enabled
    # via VERIFY_INCLUDE_DASHBOARD=1.
    include_dashboard = os.environ.get("VERIFY_INCLUDE_DASHBOARD", "0").strip().lower() in ("1", "true", "yes")
    if include_dashboard:
        routes = ["dashboard", "reels"] if preferred_verify_path == "dashboard" else ["reels", "dashboard"]
    else:
        routes = ["reels"]
    logger.info("post_trial: verify route order %s (learned=%s, dashboard=%s)",
                routes, preferred_verify_path, include_dashboard)

    # Operator C1: one short initial settle before the first confirmation attempt
    # (the reel needs a moment to appear), then quick retries inside each route.
    if verify_delay > 0:
        await asyncio.sleep(verify_delay)

    post_url: str | None = None
    dashboard_ok = False
    for route in routes:
        if route == "reels":
            url = await _capture_url_with_retry(ctx, device, url_attempts, url_retry_delay)
            if _is_reel_url(url):
                return True, url, "reels"          # live + link in hand
            post_url = post_url or url
        else:  # dashboard — already settled above, so first_delay=0
            dashboard_ok = await _confirm_via_dashboard(
                ctx, device, 0, verify_attempts, verify_retry_delay)
            if dashboard_ok:
                # confirmed; try once more for a link if reels did not already get one
                if not _is_reel_url(post_url):
                    url = await _capture_url_with_retry(ctx, device, url_attempts, url_retry_delay)
                    post_url = url or post_url
                return True, post_url, "dashboard"
    return False, post_url, None


async def post_and_track(
    *,
    device: str,
    video_url: str,
    caption: str,
    account: str | None,
    verify_delay: int,
    verify_attempts: int,
    verify_retry_delay: int,
    url_attempts: int,
    url_retry_delay: int,
    preferred_path: str | None = None,
    preferred_verify_path: str | None = None,
    on_published: Any = None,
) -> RunOutcome:
    ctx = StepContext(
        job_id=str(uuid.uuid4()),
        video_id=str(uuid.uuid4()),
        account_id="standalone",
        account_environment_id="standalone",
        device_id=device,
        mode=Mode.PROOF_OF_POSTING,
        settings={
            "device_serial": device,
            "caption_base": caption,
            "hashtags": [],
            "expected_username": account,
            "mobile_ui_executor": "mobilerun_agent",
            # Slow VN->RU link to S3: give the download generous headroom so a
            # large (median ~18 MB) video does not hit a spurious timeout->INFRA.
            "video_download_timeout": int(os.environ.get("VIDEO_DOWNLOAD_TIMEOUT", "600")),
            # Self-learning: if this account has a known-good Trial Reels entry
            # path, the goal tells the agent to try it first.
            "preferred_trial_path": preferred_path,
        },
    )

    t_start = time.monotonic()

    # 0) open Instagram cleanly so the agent starts from a known state (the run
    # force-stops it at the end, so a back-to-back run would otherwise find it
    # closed).
    await _open_instagram_clean(device)

    # 1) prepare (download presigned url, transcode, push)
    fleet_events.emit("stage_start", account=account, device=device, stage="prepare")
    t0 = time.monotonic()
    prep = await VideoPreparationStep().run(ctx, video_url=video_url, device_serial=device)
    prep_s = time.monotonic() - t0
    fleet_events.emit("stage_done", account=account, device=device, stage="prepare",
                      seconds=round(prep_s, 1), ok=prep.status == StepStatus.OK)
    if prep.status != StepStatus.OK:
        return RunOutcome(False, False, None, None, prep.code or "video_preparation_failed",
                          f"video_preparation failed: {prep.message}",
                          prep_seconds=prep_s, total_seconds=time.monotonic() - t_start)

    # 2) publish
    fleet_events.emit("stage_start", account=account, device=device, stage="publish")
    t0 = time.monotonic()
    publish = await MobileUIAutomationStep().run(ctx, device_serial=device, caption_text=caption)
    publish_s = time.monotonic() - t0
    fleet_events.emit("stage_done", account=account, device=device, stage="publish",
                      seconds=round(publish_s, 1), ok=publish.status == StepStatus.OK)
    if publish.status != StepStatus.OK:
        return RunOutcome(False, False, None, None, publish.code or "publish_failed",
                          f"publish failed: {publish.message}",
                          prep_seconds=prep_s, publish_seconds=publish_s,
                          total_seconds=time.monotonic() - t_start)

    # Which entry path (A/B/C) the agent used to reach the composer — recorded
    # for per-account self-learning. Best-effort; may be None.
    path_used: str | None = None
    try:
        md = (publish.details or {}).get("mobile_driver") or {}
        pu = md.get("path_used")
        path_used = pu if pu in ("A", "B", "C") else None
    except Exception:
        path_used = None

    # --- published: tell the caller now so it can flip DB status to
    # 'verification' BEFORE the (long) verify wait begins ---
    if on_published is not None:
        on_published()

    # 3+4) confirm the reel is live and capture its link. Self-learning: try this
    #    account's learned verify route first ('reels' deterministic capture, or
    #    'dashboard' LLM check); the other route is the fallback (operator C1:
    #    short settle + a few quick retries, not one long 180s wait).
    fleet_events.emit("stage_start", account=account, device=device, stage="verify")
    t0 = time.monotonic()
    confirmed, post_url, verify_route = await _confirm_post(
        ctx, device,
        verify_delay=verify_delay, verify_attempts=verify_attempts,
        verify_retry_delay=verify_retry_delay,
        url_attempts=url_attempts, url_retry_delay=url_retry_delay,
        preferred_verify_path=preferred_verify_path,
    )
    verify_s = time.monotonic() - t0
    fleet_events.emit("stage_done", account=account, device=device, stage="verify",
                      seconds=round(verify_s, 1), ok=confirmed)

    total_s = time.monotonic() - t_start
    return RunOutcome(
        success=confirmed,
        published=True,
        verified=confirmed,
        post_url=post_url,
        code=None if confirmed else "verification_failed",
        message=(
            f"published and confirmed via {verify_route}" if confirmed
            else "published but NOT confirmed (reels + dashboard both failed)"
        ),
        path_used=path_used,
        verify_route=verify_route,
        prep_seconds=prep_s,
        publish_seconds=publish_s,
        verify_seconds=verify_s,
        total_seconds=total_s,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _run_async(coro: Any) -> Any:
    if sys.platform == "win32":
        return asyncio.run(
            coro, loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        )
    return asyncio.run(coro)


async def _open_instagram_clean(device: str) -> None:
    """Force-stop then relaunch Instagram so the agent starts from a clean state."""
    from src.worker.tools._adb import shell as _adb_shell
    try:
        await _adb_shell(device, "am force-stop com.instagram.android", timeout=20)
        await _adb_shell(
            device,
            "monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            timeout=20,
        )
        await asyncio.sleep(4)
        print(f"opened Instagram on {device}")
    except Exception as e:  # pragma: no cover - best-effort
        logger.warning("post_trial: could not open Instagram: %s", e)


async def _close_instagram(device: str) -> None:
    """Force-stop Instagram on the device — the final cleanup after a run."""
    from src.worker.tools._adb import shell as _adb_shell
    try:
        await _adb_shell(device, "am force-stop com.instagram.android", timeout=20)
        print(f"closed Instagram on {device}")
    except Exception as e:  # pragma: no cover - cleanup must never fail a run
        logger.warning("post_trial: could not close Instagram: %s", e)


async def _drive(args: argparse.Namespace) -> int:
    # ACCOUNT DEDUP GUARD (soft pre-check; the hard per-account lock is held by the
    # loop owner — post_loop/fleet_scripted — not this per-post child). Observe by
    # default; FLEET_DEDUP_ENFORCE=1 raises before any claim.
    try:
        ai.assert_can_launch(args.device, args.account)
    except ai.DuplicateAccount as e:
        print(f"[DUP_BLOCKED] {args.account}: canonical={e.canonical} ({e.reason})")
        fleet_events.emit("result", account=args.account, device=args.device,
                          verdict="DUP_BLOCKED", rc=9, success=False, published=False,
                          code="dup_blocked")
        return 9
    # 1) claim a row
    row = claim_one(args.category)
    if row is None:
        print(f"no '{STATUS_NEW}' rows in category={args.category!r} platform={PLATFORM}")
        return 3
    vid = row["id"]
    name = row.get("name")
    print(f"claimed row id={vid} name={name} -> status={STATUS_CLAIMED}")
    fleet_events.emit("claim", account=args.account, device=args.device,
                      video_id=vid, name=name, category=args.category)

    try:
        # 2) resolve video + caption from S3
        bucket, key = _parse_s3_uri(row["link_drive"])
        folder = _batch_folder(key)
        s3 = FermaS3.from_env()
        video_url = presign(s3, bucket, key, expires=args.url_ttl)
        meta = s3.read_meta(folder)
        base_caption = (meta.caption if meta and meta.caption else row.get("caption") or "").strip()
        if not base_caption:
            raise RuntimeError(f"no caption in S3 meta for folder {folder!r} and none on the row")
        caption = uniquify_caption(base_caption)
        print(f"video s3://{bucket}/{key}\n  folder={folder} caption({len(caption)}ch)={caption!r}")

        # Self-learning: try this account's last-known-good routes first —
        # (1) the Trial Reels ENTRY path (A/B/C) for posting, and
        # (2) the VERIFY route ('reels'/'dashboard') for confirming.
        preferred = account_memory.get_preferred_path(args.account)
        if preferred:
            print(f"account {args.account}: trying learned Trial Reels path {preferred} first")
        preferred_verify = account_memory.get_preferred_verify_path(args.account)
        if preferred_verify:
            print(f"account {args.account}: trying learned verify route {preferred_verify} first")

        # 3) post + track. The moment publishing succeeds, flip the row to
        # 'verification' (with published_at) — before the verify wait.
        def _on_published() -> None:
            set_status(vid, STATUS_VERIFYING, published_at="now")
            print(f"published -> status={STATUS_VERIFYING}")
            fleet_events.emit("published", account=args.account, device=args.device,
                              video_id=vid, name=name)

        outcome = await post_and_track(
            device=args.device,
            video_url=video_url,
            caption=caption,
            account=args.account,
            verify_delay=args.verify_delay,
            verify_attempts=args.verify_attempts,
            verify_retry_delay=args.verify_retry_delay,
            url_attempts=args.url_attempts,
            url_retry_delay=args.url_retry_delay,
            preferred_path=preferred,
            preferred_verify_path=preferred_verify,
            on_published=_on_published,
        )

        # Record which entry path reached the composer so the next run for this
        # account tries it first (only meaningful once the reel actually went up).
        if outcome.published and outcome.path_used:
            account_memory.record_path(args.account, outcome.path_used)
            print(f"learned: path {outcome.path_used} works for {args.account}")
        # Record which verify route confirmed the post, same idea.
        if outcome.success and outcome.verify_route:
            account_memory.record_verify_path(args.account, outcome.verify_route)
            print(f"learned: verify route {outcome.verify_route} works for {args.account}")

        if outcome.success:
            set_status(
                vid,
                STATUS_DONE,
                link_platform=outcome.post_url,  # may be NULL if IG withheld it
                posted_by=args.account,
                published_at="now",
            )
            verdict = "SUCCESS"
            rc = 0
        elif outcome.published:
            # Published but neither dashboard-verified nor URL-captured. Do NOT
            # roll back to 'new' — the reel is live, so re-claiming it would post
            # a DUPLICATE. Leave it in 'verify' (already set, with published_at)
            # so a later confirmation pass / human can promote it to 'posted'.
            # ALWAYS stamp posted_by (even with no URL) so the live reel stays
            # traceable to its account instead of becoming an orphan row.
            set_status(vid, STATUS_VERIFYING, link_platform=outcome.post_url,
                       posted_by=args.account, published_at="now")
            verdict = "PUBLISHED_UNCONFIRMED"
            rc = 2
        else:
            # Genuine publish failure (never went live) -> roll back to 'new' for
            # retry. Leave the row's original posted_by untouched.
            set_status(vid, STATUS_NEW)
            verdict = "FAILED"
            rc = 1

        # Final step: close the Instagram app on the device.
        await _close_instagram(args.device)
        _print_report(verdict, vid, outcome, args)
        fleet_events.emit(
            "result", account=args.account, device=args.device,
            video_id=vid, name=name, verdict=verdict, rc=rc,
            success=outcome.success, published=outcome.published,
            verified=outcome.verified, post_url=outcome.post_url,
            code=outcome.code, verify_route=outcome.verify_route,
            path_used=outcome.path_used,
            timing={
                "prepare": round(outcome.prep_seconds, 1),
                "publish": round(outcome.publish_seconds, 1),
                "verify": round(outcome.verify_seconds, 1),
                "total": round(outcome.total_seconds, 1),
            },
        )
        return rc

    except Exception as e:
        logger.exception("post_trial: unexpected error; rolling row back to '%s'", STATUS_NEW)
        try:
            set_status(vid, STATUS_NEW)
        except Exception:
            logger.error("post_trial: ALSO failed to roll back row %s — needs manual fix", vid)
        await _close_instagram(args.device)
        print(f"[ERROR] {e}")
        fleet_events.emit("result", account=args.account, device=args.device,
                          video_id=vid, name=name, verdict="ERROR", rc=1,
                          success=False, published=False, code="exception",
                          error=str(e))
        return 1


def _print_report(verdict: str, vid: str, o: RunOutcome, args: argparse.Namespace) -> None:
    report = {
        "verdict": verdict,
        "video_id": vid,
        "device": args.device,
        "account": args.account,
        "published": o.published,
        "verified": o.verified,
        "post_url": o.post_url,
        "code": o.code,
        "path_used": o.path_used,
        "verify_route": o.verify_route,
        "timing_seconds": {
            "prepare": round(o.prep_seconds, 1),
            "publish": round(o.publish_seconds, 1),
            "verify": round(o.verify_seconds, 1),
            "total": round(o.total_seconds, 1),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="post_trial", description="Claim one fffbt.videos row and post it.")
    p.add_argument("--device", required=True, help="adb serial, e.g. 192.168.4.225:5555")
    p.add_argument("--category", default="trend", help="videos.category to claim (default trend).")
    p.add_argument("--account", required=True, help="IG username logged into the device (written to posted_by).")
    p.add_argument("--verify-delay", type=int, default=30, help="Seconds to settle before the first dashboard verification.")
    p.add_argument("--verify-attempts", type=int, default=3, help="Dashboard verification attempts before giving up.")
    p.add_argument("--verify-retry-delay", type=int, default=15, help="Seconds between verification attempts.")
    p.add_argument("--url-attempts", type=int, default=3, help="Post-URL capture attempts before giving up.")
    p.add_argument("--url-retry-delay", type=int, default=30, help="Seconds between URL capture attempts.")
    p.add_argument("--url-ttl", type=int, default=3600, help="Presigned URL lifetime (seconds).")
    p.set_defaults(func=_drive)
    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args(argv)
    return int(_run_async(args.func(args)))


if __name__ == "__main__":
    raise SystemExit(main())
