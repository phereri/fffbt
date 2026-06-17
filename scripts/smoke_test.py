#!/usr/bin/env python3
"""Read-only smoke test: imports, S3 presign+caption, DB claim candidate.

Mutates nothing. Touches no phone. Run with the venv python and PYTHONPATH=repo
root so `src...` resolves.
"""
from __future__ import annotations

import sys

# Ensure repo root on path even when run as scripts/smoke_test.py
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.post_trial import (  # noqa: E402
    _load_env, _mgmt_query, _parse_s3_uri, _batch_folder, presign,
    STATUS_NEW, PLATFORM,
)


def main() -> int:
    _load_env()
    print("== 1. imports of the posting path ==")
    from src.runner.post_one import post_one  # noqa: F401
    from src.worker.steps.mobile_ui_automation import MobileUIAutomationStep  # noqa: F401
    from src.worker.steps.video_preparation import VideoPreparationStep  # noqa: F401
    from src.runner.s3_source import FermaS3
    print("   OK: post_one, MobileUIAutomationStep, VideoPreparationStep, FermaS3")

    print("== 2. DB claim candidate (read-only SELECT) ==")
    rows = _mgmt_query(
        f"SELECT id, name, category, status, link_drive FROM fffbt.videos "
        f"WHERE status = '{STATUS_NEW}' AND category = 'trend' AND platform = '{PLATFORM}' "
        f"ORDER BY created_at ASC LIMIT 1"
    )
    if not rows:
        print("   NO candidate new/trend/Instagram row found")
        return 2
    row = rows[0]
    print(f"   would claim: id={row['id']} name={row['name']}")
    print(f"   link_drive={row['link_drive']}")

    print("== 3. S3 connectivity + presign + caption ==")
    s3 = FermaS3.from_env()
    folders = s3.list_folders()
    print(f"   bucket reachable: {len(folders)} folders; first few: {folders[:5]}")
    bucket, key = _parse_s3_uri(row["link_drive"])
    folder = _batch_folder(key)
    print(f"   parsed s3 uri -> bucket={bucket} key={key} folder={folder}")
    meta = s3.read_meta(folder)
    if meta and meta.caption:
        cap = meta.caption
        print(f"   meta.json caption ({len(cap)} chars): {cap!r}")
        print(f"   meta category={meta.category} platform={meta.platform}")
    else:
        print(f"   WARNING: no caption in meta.json for folder {folder!r} "
              f"(row caption would be the fallback)")
    url = presign(s3, bucket, key, expires=600)
    print(f"   presigned url (truncated): {url[:90]}...")

    print("== 4. presigned URL is fetchable (read 1 byte) ==")
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as resp:
        first = resp.read(1)
        clen = resp.headers.get("Content-Length")
        print(f"   HTTP {resp.status}; Content-Length={clen}; first byte ok={bool(first)}")

    print("\nSMOKE TEST PASSED (read-only; nothing mutated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
