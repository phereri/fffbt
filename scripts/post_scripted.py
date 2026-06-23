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
    account_links, claim_one, link_exists, presign, set_status, uniquify_caption,
)
from scripts.download_gate import DownloadGate
from scripts.router_proxy import check_proxy, serial_to_ip
from scripts.publish_trial import (
    HardStop, Traj, TrialUnavailable, _hard_stop_reason, _open_clean, a11y_ok,
    capture_link, publish, read_ui, recover_accessibility,
)
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


async def _capture_link(device: str, traj: Traj, *, attempts: int, delay: int,
                        reject: set | None = None, expect: str | None = None) -> tuple[str | None, str | None]:
    """Capture the live reel link via MULTIPLE routes, retrying (IG exposes the
    link after a beat). Returns (url, route_that_worked).

    ``reject`` holds links already saved for this account: a capture that only
    yields a known (stale) link is treated as a miss and retried, so the same
    top trial tile can never be recorded twice.

    The reel is ALREADY published here, so a blank read means the a11y service
    dropped mid-verify (the @nhienlezy115 case) -- recover it once (reboot etc.) and
    keep trying; capture_link re-opens Instagram itself after the reboot."""
    reject = reject or set()
    recovered = False
    for i in range(attempts):
        url, route = await capture_link(device, traj, reject=reject, expect=expect)
        if url and "instagram.com/reel/" in url and url not in reject:
            return url, route
        # blind capture -> if a11y is down, recover once (post is live; never re-publish)
        if not recovered:
            try:
                blind = not await a11y_ok(device)
            except Exception:
                blind = False
            if blind:
                recovered = True
                print(f"  [a11y] {device} dropped during verify -> recovering, then re-capturing")
                traj.log("a11y_down", stage="verify")
                fleet_events.emit("recover", device=device, state="start", reason="a11y_down_verify")
                ok = await recover_accessibility(device, traj)
                fleet_events.emit("recover", device=device, state="done", ok=ok)
                if ok:
                    continue                            # re-capture now, don't burn the wait
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
    traj = Traj(device, tag=account)

    # PROXY PRE-FLIGHT: a device whose proxy is down has NO internet -> IG pages
    # (Settings, Professional dashboard, the create sheet) never load, so every
    # publish fails with a navigation deviation and the device loops forever burning
    # claims + ~18 MB downloads. Ask the router to health-check this device's proxy
    # FIRST and stop the device if it's dead. Fail-open: a flaky/unreachable router
    # never blocks a run.
    if os.environ.get("PROXY_PREFLIGHT", "1").strip().lower() not in ("0", "false", "no", ""):
        chk = None
        try:
            chk = await asyncio.to_thread(check_proxy, serial_to_ip(device), 30)
            proxy_ok = bool(chk.get("working")) if isinstance(chk, dict) else True
        except Exception:
            proxy_ok = True                                 # router error -> don't block
        if not proxy_ok:
            err = (chk.get("error") if isinstance(chk, dict) else "") or "proxy not working"
            print(f"[PROXY_DOWN] {device} {account}: {err} -> skipped (no claim/download)")
            traj.log("proxy_down", stage="preflight", error=str(err))
            fleet_events.emit("result", account=account, device=device,
                              verdict="PROXY_DOWN", rc=7, success=False, published=False,
                              code="proxy_down", error=str(err))
            return 7

    # A11Y HEALTH GATE: the entire flow reads the screen through the Mobilerun
    # Portal a11y service. If it has dropped, EVERY read is blind (challenge check,
    # navigation, link capture) -> publishes fail and links can't be captured. Detect
    # it and auto-recover (toggle service, reboot, reconnect 20s/10s up to 5 min,
    # verify) BEFORE spending a claim or an ~18 MB download on a device that can't post.
    try:
        healthy = await a11y_ok(device)
    except Exception:
        healthy = True                                  # never block a run on the check itself
    if not healthy:
        print(f"[a11y] {device} accessibility service is DOWN -> recovering")
        traj.log("a11y_down", stage="preflight")
        fleet_events.emit("recover", account=account, device=device,
                          state="start", reason="a11y_down")
        ok = await recover_accessibility(device, traj)
        fleet_events.emit("recover", account=account, device=device, state="done", ok=ok)
        if not ok:
            print(f"[A11Y_DOWN] {device} {account}: a11y still down after recovery -> skipped (no claim)")
            fleet_events.emit("result", account=account, device=device,
                              verdict="A11Y_DOWN", rc=5, success=False, published=False,
                              code="a11y_down")
            return 5
        print(f"[a11y] {device} recovered -> continuing")

    # PRE-FLIGHT: detect a login challenge / checkpoint BEFORE claiming a row or
    # downloading an ~18 MB video — a blocked device must cost nothing. Best-effort:
    # if the screen can't be read, proceed (the in-flow check still guards publish).
    try:
        await _open_clean(device, traj)
        pf_nodes = await read_ui(device)
        hs = _hard_stop_reason(pf_nodes)
    except Exception:
        pf_nodes, hs = [], None
    if hs:
        reason, marker = hs
        traj.deviation("hard_stop/preflight", pf_nodes, note=f"{reason}: {marker}")
        traj.log("login_challenge", reason=reason, marker=marker, stage="preflight")
        print(f"[BLOCKED] {device} {account}: {reason} ({marker}) -> skipped (no claim/prep); traj={traj.dir}")
        fleet_events.emit("result", account=account, device=device,
                          verdict="BLOCKED", rc=4, success=False, published=False,
                          code=reason, error=marker)
        try:
            await _close_instagram(device)
        except Exception:
            pass
        return 4

    row = claim_one(args.category, getattr(args, "order", "asc"))
    if row is None:
        print(f"no '{STATUS_NEW}' rows in category={args.category!r}")
        return 3
    vid = row["id"]
    name = row.get("name")
    print(f"claimed row id={vid} name={name} -> status=posting")
    fleet_events.emit("claim", account=account, device=device,
                      video_id=vid, name=name, category=args.category)
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

        # 2) push the real video to the gallery — through the fleet-wide DOWNLOAD
        # QUEUE: only one device downloads at a time (the slow S3 link collapses
        # when several run at once). Sitting in the queue is NORMAL and untimed;
        # the 360s timeout applies only once it's our turn and the download runs.
        ctx = StepContext(
            job_id=str(uuid.uuid4()), video_id=str(uuid.uuid4()),
            account_id="scripted", account_environment_id="scripted", device_id=device,
            mode=Mode.PROOF_OF_POSTING,
            settings={"device_serial": device,
                      "video_download_timeout": int(os.environ.get("VIDEO_DOWNLOAD_TIMEOUT", "360"))},
        )
        fleet_events.emit("stage_start", account=account, device=device, stage="queue")
        traj.log("download_queued")
        print("  [queue] waiting for download slot…")
        qt0 = time.monotonic()
        async with DownloadGate():
            queued_s = time.monotonic() - qt0
            fleet_events.emit("stage_start", account=account, device=device, stage="prepare",
                              queued_seconds=round(queued_s, 1))
            traj.log("download_started", queued_seconds=round(queued_s, 1))
            print(f"  [queue] our turn after {queued_s:.0f}s — downloading")
            t0 = time.monotonic()
            prep = await VideoPreparationStep().run(ctx, video_url=video_url, device_serial=device)
            prep_s = time.monotonic() - t0
        fleet_events.emit("stage_done", account=account, device=device, stage="prepare",
                          seconds=round(prep_s, 1), queued_seconds=round(queued_s, 1),
                          ok=prep.status == StepStatus.OK)
        traj.log("prepare_done", ok=prep.status == StepStatus.OK, seconds=round(prep_s, 1),
                 queued_seconds=round(queued_s, 1), gallery=ctx.settings.get("host_video_in_gallery"))
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

        # published — flip to verify with published_at BEFORE the link wait. Record
        # the EXACT caption we posted (posted_caption): it uniquely identifies this
        # reel later (the base caption repeats across videos), so a future
        # link recovery can match a reel to its row without guessing.
        set_status(vid, STATUS_VERIFYING, published_at="now", posted_caption=caption)
        fleet_events.emit("published", account=account, device=device, video_id=vid, name=name)
        print("published -> status=verify")

        # 4) capture the live reel link. Reject links already saved for this
        # account so we never record the same (stale) top trial tile twice.
        known_links = account_links(account)
        fleet_events.emit("stage_start", account=account, device=device, stage="verify")
        t0 = time.monotonic()
        url, route = await _capture_link(device, traj, attempts=args.url_attempts,
                                         delay=args.url_retry_delay, reject=known_links,
                                         expect=caption)
        verify_s = time.monotonic() - t0
        fleet_events.emit("stage_done", account=account, device=device, stage="verify",
                          seconds=round(verify_s, 1), ok=bool(url))

        # Final guard: the link must be globally new. link_exists() also keeps the
        # write off the unique index's toes (a violating UPDATE would error and
        # wrongly roll the row back to 'new', re-posting a duplicate video).
        if url and not link_exists(url):
            set_status(vid, STATUS_DONE, link_platform=url, posted_by=account, published_at="now")
            verdict, rc = "SUCCESS", 0
            print(f"[SUCCESS] live link ({route}): {url} -> status=posted")
        else:
            # Either nothing captured, or only a duplicate surfaced. Do NOT write a
            # dup link and do NOT roll back (would re-post). Leave it in verify.
            if url:
                traj.log("capture_duplicate", url=url, note="captured link already saved -> left empty")
                fleet_events.emit("dup_link", account=account, device=device,
                                  video_id=vid, name=name, url=url)
                print(f"[PUBLISHED_UNCONFIRMED] captured link {url} already exists -> left empty (no dup)")
            else:
                print("[PUBLISHED_UNCONFIRMED] reel is live but link not captured; left in verify")
            set_status(vid, STATUS_VERIFYING, posted_by=account, published_at="now")
            verdict, rc = "PUBLISHED_UNCONFIRMED", 2

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

    except HardStop as e:
        # login challenge / checkpoint / block: STOP this device, never keep
        # tapping. The video was NOT posted -> release it so a healthy device can
        # take it. Flag the account clearly (trajectory has the full screen dump).
        try:
            set_status(vid, STATUS_NEW)
        except Exception:
            pass
        try:
            traj.log("login_challenge", reason=e.reason, marker=e.marker)
        except Exception:
            pass
        print(f"[BLOCKED] {device} {account}: {e.reason} ({e.marker}) -> run stopped; traj={traj.dir}")
        fleet_events.emit("result", account=account, device=device, video_id=vid, name=name,
                          verdict="BLOCKED", rc=4, success=False, published=False,
                          code=e.reason, error=e.marker)
        return 4

    except TrialUnavailable as e:
        # neither path to a trial reel exists on this account -> STOP this device and
        # release the row. Not a failure of the video; the account just can't post
        # trial reels (e.g. not eligible / no Professional 'Trial reels').
        try:
            set_status(vid, STATUS_NEW)
        except Exception:
            pass
        try:
            traj.log("trial_unavailable", detail=e.detail)
        except Exception:
            pass
        print(f"[TRIAL_UNAVAILABLE] {device} {account}: {e.detail} -> stopping device; traj={traj.dir}")
        fleet_events.emit("result", account=account, device=device, video_id=vid, name=name,
                          verdict="TRIAL_UNAVAILABLE", rc=6, success=False, published=False,
                          code="trial_unavailable", error=e.detail)
        return 6

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
    p.add_argument("--order", choices=("asc", "desc"), default="asc",
                   help="claim oldest-first (asc) or newest-first (desc)")
    p.add_argument("--url-ttl", type=int, default=3600)
    p.add_argument("--url-attempts", type=int, default=5)     # wait+retry up to 5x
    p.add_argument("--url-retry-delay", type=int, default=30)  # 30s between attempts
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
