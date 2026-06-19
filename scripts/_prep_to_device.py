#!/usr/bin/env python3
"""Test helper: prepare one trend video onto a device's gallery (no DB claim).

Picks a 'new' trend row's link_drive read-only, presigns it, and runs the same
VideoPreparationStep (download -> transcode -> push) used by post_trial — so a
device has a real video to publish for testing scripts/publish_trial.py.

Usage: python scripts/_prep_to_device.py <serial>
Prints the pushed gallery filename + the batch caption.
"""
from __future__ import annotations

import asyncio
import json
import os
import selectors
import sys
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.post_trial import (  # reuse the proven helpers
    _batch_folder, _load_env, _mgmt_query, _parse_s3_uri, presign, uniquify_caption,
)
from src.runner.s3_source import FermaS3
from src.worker.session.types import Mode, StepContext, StepStatus
from src.worker.steps.video_preparation import VideoPreparationStep


async def main():
    serial = sys.argv[1]
    _load_env()
    row = _mgmt_query(
        "SELECT id, link_drive FROM fffbt.videos "
        "WHERE status='new' AND category='trend' AND platform='Instagram' "
        "ORDER BY created_at ASC LIMIT 1"
    )[0]
    bucket, key = _parse_s3_uri(row["link_drive"])
    folder = _batch_folder(key)
    s3 = FermaS3.from_env()
    url = presign(s3, bucket, key, expires=3600)
    meta = s3.read_meta(folder)
    caption = uniquify_caption((meta.caption if meta and meta.caption else "test").strip())
    print(f"video s3://{bucket}/{key}  folder={folder}")
    ctx = StepContext(
        job_id=str(uuid.uuid4()), video_id=str(uuid.uuid4()),
        account_id="t", account_environment_id="t", device_id=serial,
        mode=Mode.PROOF_OF_POSTING,
        settings={"device_serial": serial, "video_download_timeout": 600},
    )
    prep = await VideoPreparationStep().run(ctx, video_url=url, device_serial=serial)
    print("prepare:", prep.status, "->", ctx.settings.get("host_video_in_gallery"))
    print("CAPTION:", caption)
    return 0 if prep.status == StepStatus.OK else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
