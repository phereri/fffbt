#!/usr/bin/env python3
"""Standalone CLI: publish one Trial Reel to a prepared device — no database.

Usage:
    python -m runner <command> [options]

Commands:
    post-one   Publish one video to Instagram Trial Reels on one device.
    devices    List adb devices (and optionally adb-connect a LAN/Tailscale IP).
    s3         Inspect / pull videos from the Ferma S3 bucket (ls, meta, pull).

This entrypoint is deliberately independent of ``scheduler.cli``: it needs no
Supabase connection, no GenFarmer, and touches no identity / proxy state. It is
meant to be run from a fresh clone on the PC that has the phones attached.

Run any command with --help for details.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import selectors
import subprocess
import sys
from typing import Any

from src.runner.post_one import post_one


def _run_async(coro: Any) -> Any:
    """Run a coroutine on a Windows-safe event loop.

    Mirrors ``scheduler.cli._run_async``: the Windows default
    ProactorEventLoop breaks some of the libraries used downstream, so force a
    SelectorEventLoop on win32.
    """
    if sys.platform == "win32":
        return asyncio.run(
            coro,
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    return asyncio.run(coro)


def _adb_path() -> str:
    return os.environ.get("ADB_PATH", "adb")


# ---------------------------------------------------------------------------
# post-one
# ---------------------------------------------------------------------------


def _cmd_post_one(args: argparse.Namespace) -> int:
    hashtags = _split_hashtags(args.hashtags)
    result = _run_async(
        post_one(
            device_serial=args.device,
            video=args.video,
            caption=args.caption,
            hashtags=hashtags,
            expected_username=args.account,
            verify=not args.no_verify,
            verify_delay_seconds=args.verify_delay,
            capture_url=not args.no_url,
            bucket_video_id=args.video_id,
            category=args.category,
            source_key=args.source_key,
            log_path=args.log,
        )
    )

    if args.json:
        print(
            json.dumps(
                {
                    "success": result.success,
                    "published": result.published,
                    "verified": result.verified,
                    "post_url": result.post_url,
                    "message": result.message,
                    "code": result.code,
                    "details": result.details,
                },
                indent=2,
            )
        )
    else:
        verdict = "SUCCESS" if result.success else "FAILED"
        print(f"[{verdict}] {result.message}")
        print(
            f"  published={result.published} verified={result.verified} "
            f"url={result.post_url} code={result.code}"
        )

    return 0 if result.success else 1


def _split_hashtags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tok for tok in raw.replace(",", " ").split() if tok]


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


def _cmd_devices(args: argparse.Namespace) -> int:
    adb = _adb_path()
    if args.connect:
        try:
            out = subprocess.run(
                [adb, "connect", args.connect],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            print((out.stdout or out.stderr or "").strip())
        except Exception as e:
            print(f"adb connect failed: {e}", file=sys.stderr)
            return 1

    try:
        out = subprocess.run(
            [adb, "devices", "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print(
            f"error: adb not found at {adb!r}. Set ADB_PATH or install "
            "platform-tools.",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        print(f"adb devices failed: {e}", file=sys.stderr)
        return 1

    print((out.stdout or "").rstrip())
    return 0


# ---------------------------------------------------------------------------
# s3
# ---------------------------------------------------------------------------


def _s3_client():
    from src.runner.s3_source import FermaS3

    return FermaS3.from_env()


def _cmd_s3_ls(args: argparse.Namespace) -> int:
    s3 = _s3_client()
    if args.video_id:
        folder = s3.get_folder(args.video_id)
        if args.json:
            print(
                json.dumps(
                    {
                        "video_id": folder.video_id,
                        "prefix": folder.prefix,
                        "videos": folder.video_keys,
                        "meta": folder.meta.raw if folder.meta else None,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"{folder.video_id}  ({len(folder.video_keys)} videos)")
            for key in folder.video_keys:
                print(f"  {key}")
            if folder.meta:
                print(
                    f"  meta: platform={folder.meta.platform} "
                    f"category={folder.meta.category} "
                    f"caption={folder.meta.caption!r}"
                )
        return 0

    folders = s3.list_folders()
    if args.json:
        print(json.dumps(folders, indent=2, ensure_ascii=False))
    else:
        for name in folders:
            print(name)
        print(f"\n{len(folders)} folder(s) under {s3.config.prefix}")
    return 0


def _cmd_s3_meta(args: argparse.Namespace) -> int:
    s3 = _s3_client()
    meta = s3.read_meta(args.video_id)
    if meta is None:
        print(f"no meta.json for {args.video_id!r}", file=sys.stderr)
        return 1
    print(json.dumps(meta.raw, indent=2, ensure_ascii=False))
    return 0


def _cmd_s3_pull(args: argparse.Namespace) -> int:
    if not args.key and not args.video_id:
        print("error: pull needs a video_id or --key", file=sys.stderr)
        return 2
    s3 = _s3_client()
    if args.key:
        # Pull a single explicit key.
        dest = args.dest or os.path.basename(args.key)
        path = s3.download(args.key, dest)
        print(str(path))
        return 0

    # Pull videos for a whole folder (optionally just the first N).
    folder = s3.get_folder(args.video_id)
    if not folder.video_keys:
        print(f"no videos in {args.video_id!r}", file=sys.stderr)
        return 1
    keys = folder.video_keys
    if args.limit is not None:
        keys = keys[: args.limit]
    out_dir = args.dest or args.video_id
    for key in keys:
        dest = os.path.join(out_dir, os.path.basename(key))
        path = s3.download(key, dest)
        print(str(path))
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runner",
        description="Standalone Trial Reel poster (no database).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_post = sub.add_parser(
        "post-one",
        help="Publish one video to Trial Reels on one device.",
    )
    p_post.add_argument(
        "--device",
        required=True,
        help="adb serial / TCP address, e.g. 100.100.57.41:5555",
    )
    p_post.add_argument(
        "--video",
        required=True,
        help="Local .mp4 path OR an http(s)/S3 presigned URL.",
    )
    p_post.add_argument("--caption", required=True, help="Caption body text.")
    p_post.add_argument(
        "--hashtags",
        default=None,
        help="Comma/space separated hashtags (with or without leading #).",
    )
    p_post.add_argument(
        "--account",
        "--expected-username",
        dest="account",
        default=None,
        help="Optional: the IG username expected on the device (logged + informational).",
    )
    # Provenance — recorded verbatim in the posted-reels log so the resulting
    # link can be tied back to the source video. None of these affect posting.
    p_post.add_argument(
        "--video-id",
        default=None,
        help="Bucket folder name this video came from (e.g. Cowboy).",
    )
    p_post.add_argument(
        "--category",
        default=None,
        help="Category from the folder's meta.json (e.g. trend, mems).",
    )
    p_post.add_argument(
        "--source-key",
        default=None,
        help="Full S3 key of the exact video file (e.g. ferma/Cowboy/VID_x.mp4).",
    )
    p_post.add_argument(
        "--log",
        default=None,
        help="Posted-reels JSONL log path (default $POSTED_REELS_LOG or "
        "posted_reels.jsonl).",
    )
    p_post.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the delayed Professional-dashboard confirmation.",
    )
    p_post.add_argument(
        "--verify-delay",
        type=int,
        default=180,
        help="Seconds to wait before dashboard verification (default 180).",
    )
    p_post.add_argument(
        "--no-url",
        action="store_true",
        help="Skip best-effort post-URL capture after publishing.",
    )
    p_post.add_argument("--json", action="store_true", help="Emit JSON result.")
    p_post.set_defaults(func=_cmd_post_one)

    p_dev = sub.add_parser(
        "devices",
        help="List adb devices (optionally adb-connect a LAN/Tailscale IP first).",
    )
    p_dev.add_argument(
        "--connect",
        default=None,
        help="adb connect this ip:port before listing (e.g. 100.100.57.41:5555).",
    )
    p_dev.set_defaults(func=_cmd_devices)

    # --- s3 ---------------------------------------------------------------
    p_s3 = sub.add_parser(
        "s3",
        help="Inspect / pull videos from the Ferma S3 bucket.",
    )
    s3_sub = p_s3.add_subparsers(dest="s3_command", required=True)

    p_ls = s3_sub.add_parser(
        "ls",
        help="List video_id folders, or the contents of one folder.",
    )
    p_ls.add_argument(
        "video_id",
        nargs="?",
        default=None,
        help="Optional: a folder name to list its videos + meta.",
    )
    p_ls.add_argument("--json", action="store_true", help="Emit JSON.")
    p_ls.set_defaults(func=_cmd_s3_ls)

    p_meta = s3_sub.add_parser(
        "meta",
        help="Print a folder's meta.json.",
    )
    p_meta.add_argument("video_id", help="Folder name.")
    p_meta.set_defaults(func=_cmd_s3_meta)

    p_pull = s3_sub.add_parser(
        "pull",
        help="Download videos from a folder (or one explicit key) to disk.",
    )
    p_pull.add_argument(
        "video_id",
        nargs="?",
        default=None,
        help="Folder to download videos from.",
    )
    p_pull.add_argument(
        "--key",
        default=None,
        help="Download one explicit S3 key instead of a whole folder.",
    )
    p_pull.add_argument(
        "--dest",
        default=None,
        help="Output dir (folder mode) or file path (--key mode).",
    )
    p_pull.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Folder mode: download only the first N videos.",
    )
    p_pull.set_defaults(func=_cmd_s3_pull)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
