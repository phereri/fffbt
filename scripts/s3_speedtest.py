#!/usr/bin/env python3
"""Manually measure download speed from the Ferma S3 (TWC) storage.

Reads FERMA_S3_* from .env, picks real video object(s) from the bucket, and
times a plain single-stream HTTPS GET of a presigned URL — exactly how the
poster downloads videos. Prints MB/s + Mbps per file and the average.

Usage:
  python scripts/s3_speedtest.py                # download 1 video, report speed
  python scripts/s3_speedtest.py --count 3      # average over 3 videos
  python scripts/s3_speedtest.py --url          # just print a presigned URL
                                                #   (then: curl -o NUL "<url>")
  python scripts/s3_speedtest.py --max-secs 15  # cap each download at 15s
"""
from __future__ import annotations

import argparse
import os
import time
import urllib.request


def _load_env(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1, help="how many videos to test")
    ap.add_argument("--max-secs", type=int, default=0, help="cap each download (0 = full file)")
    ap.add_argument("--url", action="store_true", help="just print a presigned URL and exit")
    args = ap.parse_args()

    env = _load_env()
    import boto3
    from botocore.client import Config

    endpoint = env.get("FERMA_S3_ENDPOINT", "https://s3.twcstorage.ru")
    bucket = env.get("FERMA_S3_BUCKET", "neiroslop")
    prefix = env.get("FERMA_S3_PREFIX", "ferma/")
    c = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=env["FERMA_S3_ACCESS_KEY"],
        aws_secret_access_key=env["FERMA_S3_SECRET_KEY"],
        region_name=env.get("FERMA_S3_REGION", "ru-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    listing = c.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=400)
    vids = [o["Key"] for o in listing.get("Contents", []) if o["Key"].lower().endswith(".mp4")]
    if not vids:
        print("no .mp4 objects found under", prefix)
        return 1

    if args.url:
        url = c.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": vids[0]}, ExpiresIn=600)
        print("# presigned URL (valid 10 min) — test with:  curl -o NUL \"<url>\"")
        print(url)
        return 0

    print(f"endpoint={endpoint}  bucket={bucket}")
    speeds = []
    for i in range(args.count):
        key = vids[i % len(vids)]
        url = c.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=600)
        t = time.time(); got = 0
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                while True:
                    ch = r.read(1 << 20)
                    if not ch:
                        break
                    got += len(ch)
                    if args.max_secs and time.time() - t > args.max_secs:
                        break
        except Exception as e:
            print(f"  [{i}] {key}: ERROR {e!r}")
            continue
        dt = max(time.time() - t, 1e-6)
        mbps = got * 8 / 1e6 / dt
        speeds.append(mbps)
        print(f"  [{i}] {got/1e6:5.1f} MB in {dt:5.1f}s = {got/1e6/dt:5.2f} MB/s = {mbps:6.2f} Mbps   {key}")

    if speeds:
        avg = sum(speeds) / len(speeds)
        print(f"\nAVG: {avg:.2f} Mbps  ({avg/8:.2f} MB/s) over {len(speeds)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
