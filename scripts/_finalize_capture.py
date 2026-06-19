#!/usr/bin/env python3
"""Capture the link for an already-published (status=verify) reel and finalize it.

For a row whose reel went LIVE but whose run was interrupted before link capture:
capture the link via the multi-route capture, then set status=posted. Never rolls
back (the reel is live -> re-posting would duplicate).

Usage: python scripts/_finalize_capture.py <serial> <video_id> <account> [attempts]
"""
from __future__ import annotations

import asyncio
import selectors
import sys

sys.path.insert(0, ".")

from scripts.post_trial import _load_env, set_status, STATUS_DONE
from scripts.publish_trial import Traj, capture_link

_load_env()


async def main():
    serial, vid, account = sys.argv[1], sys.argv[2], sys.argv[3]
    attempts = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    traj = Traj(serial, tag=f"{account}_finalize")
    url, route = None, None
    for i in range(attempts):
        url, route = await capture_link(serial, traj)
        if url:
            break
        print(f"  no link yet ({i + 1}/{attempts})")
        if i + 1 < attempts:
            await asyncio.sleep(20)
    if url:
        set_status(vid, STATUS_DONE, link_platform=url, posted_by=account, published_at="now")
        print(f"[SUCCESS] {serial} {account}: {url} (route={route}) -> status=posted")
        return 0
    print(f"[UNCONFIRMED] {serial} {account}: link not captured; left in verify (traj={traj.dir})")
    return 2


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
