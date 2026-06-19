#!/usr/bin/env python3
"""Full scripted (no-agent) Trial-Reel post of ONE claimed fffbt.videos row.

End-to-end, deterministic — NO MobileRun agent, NO LLM for the UI:
  1. claim one 'new' trend row atomically (status -> 'posting');
  2. resolve the S3 video + uniquify its meta.json caption;
  3. push the real video to the device gallery (VideoPreparationStep);
  4. publish it as a Trial Reel via the deterministic publisher
     (scripts/publish_trial.publish) with humanized 7-15s action delays;
  5. capture the live reel link deterministically (no hallucination);
  6. write the result back to the DB (status -> 'posted', link, posted_by).

This is the scripted replacement for post_trial's agent path. The caption is
always the real, uniquified meta.json caption — never a placeholder.

Usage:
  python scripts/post_scripted.py --device 192.168.5.191:5555 [--category trend]
The account (posted_by) is resolved from data/device_accounts.json by serial.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.post_trial import (
    STATUS_DONE, STATUS_NEW, STATUS_VERIFYING,
    _batch_folder, _close_instagram, _load_env, _parse_s3_uri,
    claim_one, presign, set_status, uniquify_caption,
)
from scripts.publish_trial import Traj, capture_link, publish
from src.runner import fleet_events
from src.runner.s3_source import FermaS3
from src.worker.session.types import Mode, StepContext, StepStatus
from src.worker.steps.video_preparation import VideoPreparationStep


def _account_for(serial: str) -> str | None:
    p = Path("data/device_accounts.json")
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return (data.get("devices") or {}).get(serial)


async def _capture_link(device: str, traj: Traj, *, attempts: int, delay: int) -> tuple[str | None, str | None]:
    """Capture the live reel link via MULTIPLE routes, retrying (IG exposes the
    link after a beat). Returns (url, route_that_worked)."""
    for i in range(attempts):
        url, route = await capture_link(device, traj)
        if url and "instagram.com/reel/" in url:
            return url, route
        if i + 1 < attempts:
            traj.log("capture_retry", attempt=i + 1, of=attempts, wait=delay)
            print(f"  link not ready, retry in {delay}s ({i + 1}/{attempts})")
            await asyncio.sleep(delay)
    return None, None


async def _drive(args: argparse.Namespace) -> int:
    device = args.device
    account = _account_for(device)
    if not account:
        print(f"[ERROR] no account bound to {device} in data/device_accounts.json")
        return 1
    print(f"device={device} account={account} category={args.category}")

    row = claim_one(args.category)
    if row is None:
        print(f"no '{STATUS_NEW}' rows in category={args.category!r}")
        return 3
    vid = row["id"]
    name = row.get("name")
    print(f"claimed row id={vid} name={name} -> status=posting")
    fleet_events.emit("claim", account=account, device=device,
                      video_id=vid, name=name, category=args.category)

    traj = Traj(device, tag=account)
    traj.log("run_start", account=account, video_id=vid, name=name, category=args.category)
    print(f"trajectory: {traj.dir}")

    t_start = time.monotonic()
    try:
        # 1) resolve video + real uniquified caption
        bucket, key = _parse_s3_uri(row["link_drive"])
        folder = _batch_folder(key)
        s3 = FermaS3.from_env()
        video_url = presign(s3, bucket, key, expires=args.url_ttl)
        meta = s3.read_meta(folder)
        base_caption = (meta.caption if meta and meta.caption else row.get("caption") or "").strip()
        if not base_caption:
            raise RuntimeError(f"no caption in S3 meta for {folder!r} and none on the row")
        caption = uniquify_caption(base_caption)
        print(f"video s3://{bucket}/{key}\n  caption({len(caption)}ch)={caption!r}")

        # 2) push the real video to the gallery
        ctx = StepContext(
            job_id=str(uuid.uuid4()), video_id=str(uuid.uuid4()),
            account_id="scripted", account_environment_id="scripted", device_id=device,
            mode=Mode.PROOF_OF_POSTING,
            settings={"device_serial": device,
                      "video_download_timeout": int(os.environ.get("VIDEO_DOWNLOAD_TIMEOUT", "600"))},
        )
        fleet_events.emit("stage_start", account=account, device=device, stage="prepare")
        t0 = time.monotonic()
        prep = await VideoPreparationStep().run(ctx, video_url=video_url, device_serial=device)
        prep_s = time.monotonic() - t0
        fleet_events.emit("stage_done", account=account, device=device, stage="prepare",
                          seconds=round(prep_s, 1), ok=prep.status == StepStatus.OK)
        traj.log("prepare_done", ok=prep.status == StepStatus.OK, seconds=round(prep_s, 1),
                 gallery=ctx.settings.get("host_video_in_gallery"))
        if prep.status != StepStatus.OK:
            traj.log("prepare_fail", message=str(prep.message))
            set_status(vid, STATUS_NEW)
            print(f"[FAIL] prepare: {prep.message} -> rolled back to new")
            return 1

        # 3) publish deterministically with the REAL caption + humanized delays
        fleet_events.emit("stage_start", account=account, device=device, stage="publish")
        t0 = time.monotonic()
        res = await publish(device, caption, no_share=args.no_share, traj=traj)
        publish_s = time.monotonic() - t0
        fleet_events.emit("stage_done", account=account, device=device, stage="publish",
                          seconds=round(publish_s, 1), ok=bool(res.get("ok")))
        print(f"publish result: {res} ({publish_s:.0f}s)")

        if args.no_share:
            # dry-run never reached Share; release the row for a real run later.
            set_status(vid, STATUS_NEW)
            print("[dry-run] released row back to new (no publish)")
            return 0 if res.get("ok") else 1

        if not res.get("ok"):
            set_status(vid, STATUS_NEW)
            print("[FAIL] publish did not reach a published state -> rolled back to new")
            return 1

        # published — flip to verify with published_at BEFORE the link wait
        set_status(vid, STATUS_VERIFYING, published_at="now")
        fleet_events.emit("published", account=account, device=device, video_id=vid, name=name)
        print("published -> status=verify")

        # 4) capture the live reel link
        fleet_events.emit("stage_start", account=account, device=device, stage="verify")
        t0 = time.monotonic()
        url, route = await _capture_link(device, traj, attempts=args.url_attempts, delay=args.url_retry_delay)
        verify_s = time.monotonic() - t0
        fleet_events.emit("stage_done", account=account, device=device, stage="verify",
                          seconds=round(verify_s, 1), ok=bool(url))

        if url:
            set_status(vid, STATUS_DONE, link_platform=url, posted_by=account, published_at="now")
            verdict, rc = "SUCCESS", 0
            print(f"[SUCCESS] live link ({route}): {url} -> status=posted")
        else:
            # live but link not captured: do NOT roll back (would re-post a dup).
            set_status(vid, STATUS_VERIFYING, posted_by=account, published_at="now")
            verdict, rc = "PUBLISHED_UNCONFIRMED", 2
            print("[PUBLISHED_UNCONFIRMED] reel is live but link not captured; left in verify")

        traj.log("run_result", verdict=verdict, rc=rc, post_url=url, verify_route=route,
                 deviations=traj.deviations,
                 timing={"prepare": round(prep_s, 1), "publish": round(publish_s, 1),
                         "verify": round(verify_s, 1), "total": round(time.monotonic() - t_start, 1)})
        print(f"[{verdict}] {device} {account}  deviations={traj.deviations}  traj={traj.dir}")
        fleet_events.emit("result", account=account, device=device, video_id=vid, name=name,
                          verdict=verdict, rc=rc, success=rc == 0, published=True,
                          post_url=url, verify_route=route,
                          timing={"prepare": round(prep_s, 1), "publish": round(publish_s, 1),
                                  "verify": round(verify_s, 1),
                                  "total": round(time.monotonic() - t_start, 1)})
        return rc

    except Exception as e:
        try:
            traj.log("run_error", error=str(e))
        except Exception:
            pass
        try:
            set_status(vid, STATUS_NEW)
        except Exception:
            print(f"[ERROR] ALSO failed to roll back row {vid} — needs manual fix")
        print(f"[ERROR] {e}")
        fleet_events.emit("result", account=account, device=device, video_id=vid, name=name,
                          verdict="ERROR", rc=1, success=False, published=False,
                          code="exception", error=str(e))
        return 1

    finally:
        # ALWAYS close Instagram at the end of a run (operator rule), regardless of
        # outcome — leave the device clean rather than parked deep in the app.
        try:
            await _close_instagram(device)
        except Exception:
            pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="post_scripted")
    p.add_argument("--device", required=True, help="adb serial, e.g. 192.168.5.191:5555")
    p.add_argument("--category", default="trend")
    p.add_argument("--url-ttl", type=int, default=3600)
    p.add_argument("--url-attempts", type=int, default=4)
    p.add_argument("--url-retry-delay", type=int, default=30)
    p.add_argument("--no-share", action="store_true",
                   help="dry-run: validate flow up to (not including) Share, release row")
    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _build_parser().parse_args(argv)
    if sys.platform == "win32":
        return int(asyncio.run(_drive(args),
                               loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    return int(asyncio.run(_drive(args)))


if __name__ == "__main__":
    raise SystemExit(main())
