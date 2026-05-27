#!/usr/bin/env python3
"""Seed one local validation MP4 into automation.videos.

This script is intentionally DB-only. It validates a local MP4 path and creates
or refreshes a single validation video row. It does not create jobs, touch
Google Drive rows, mutate devices, run posting, or publish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Any


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _load_dotenv(path: pathlib.Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _management_api_query(project_ref: str, pat: str, sql: str) -> list[dict[str, Any]]:
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "User-Agent": "fffbt-validation-video-seed/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Management API query failed ({exc.code}): {detail}") from None
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected Management API response: {data!r}")
    return data


def _seed_sql(path: pathlib.Path, *, checksum: str, size_bytes: int) -> str:
    filename = path.name
    extension = path.suffix.lstrip(".").lower() or "mp4"
    mime_type = mimetypes.guess_type(str(path))[0] or "video/mp4"
    local_path = str(path)
    source_path = f"validation://{filename}"
    return f"""
WITH existing AS (
    SELECT id
    FROM automation.videos
    WHERE local_video_path = {_sql_literal(local_path)}
      AND category = 'validation'
    LIMIT 1
),
inserted AS (
    INSERT INTO automation.videos (
        google_drive_file_id,
        google_drive_folder_id,
        source_path,
        filename,
        extension,
        mime_type,
        size_bytes,
        checksum,
        platform,
        category,
        status,
        local_video_path,
        download_method
    )
    SELECT
        NULL,
        NULL,
        {_sql_literal(source_path)},
        {_sql_literal(filename)},
        {_sql_literal(extension)},
        {_sql_literal(mime_type)},
        {int(size_bytes)},
        {_sql_literal(checksum)},
        'instagram',
        'validation',
        'new',
        {_sql_literal(local_path)},
        'local_validation'
    WHERE NOT EXISTS (SELECT 1 FROM existing)
    RETURNING id, true AS created
),
refreshed AS (
    UPDATE automation.videos
       SET status = 'new',
           platform = 'instagram',
           category = 'validation',
           source_path = {_sql_literal(source_path)},
           filename = {_sql_literal(filename)},
           extension = {_sql_literal(extension)},
           mime_type = {_sql_literal(mime_type)},
           size_bytes = {int(size_bytes)},
           checksum = {_sql_literal(checksum)},
           local_video_path = {_sql_literal(local_path)},
           download_method = 'local_validation'
     WHERE id = (SELECT id FROM existing)
       AND NOT EXISTS (
           SELECT 1 FROM automation.jobs j
           WHERE j.video_id = (SELECT id FROM existing)
             AND j.status NOT IN ('done', 'failed', 'cancelled')
       )
    RETURNING id, false AS created
),
row AS (
    SELECT * FROM inserted
    UNION ALL
    SELECT * FROM refreshed
)
SELECT
    id::text AS video_id,
    created,
    'instagram' AS platform,
    'validation' AS category,
    'new' AS status,
    {_sql_literal(local_path)} AS local_video_path,
    {_sql_literal(filename)} AS filename,
    {int(size_bytes)} AS size_bytes,
    {_sql_literal(checksum)} AS checksum
FROM row;
"""


def _run_direct(db_url: str, sql: str) -> list[dict[str, Any]]:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError("psycopg is required for direct DB mode") from exc
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            keys = [desc.name for desc in cur.description]
        conn.commit()
    return [dict(zip(keys, row)) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a local MP4 as one validation automation.videos row."
    )
    parser.add_argument("mp4_path", help="Local MP4 path on this VPS.")
    parser.add_argument("--db-url", default=os.environ.get("SUPABASE_DB_URL"))
    parser.add_argument("--via-management-api", action="store_true")
    parser.add_argument("--project-ref", default=os.environ.get("SUPABASE_PROJECT_REF"))
    args = parser.parse_args(argv)

    _load_dotenv(pathlib.Path(".env"))

    path = pathlib.Path(args.mp4_path).expanduser().resolve()
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    if path.suffix.lower() != ".mp4":
        print(f"error: expected an .mp4 file: {path}", file=sys.stderr)
        return 2
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        print(f"error: file is empty: {path}", file=sys.stderr)
        return 2

    sql = _seed_sql(path, checksum=_sha256(path), size_bytes=size_bytes)
    if args.via_management_api:
        pat = os.environ.get("SUPABASE_PAT")
        project_ref = args.project_ref or os.environ.get("SUPABASE_PROJECT_REF")
        if not pat or not project_ref:
            print(
                "error: SUPABASE_PAT and --project-ref/SUPABASE_PROJECT_REF are required.",
                file=sys.stderr,
            )
            return 2
        rows = _management_api_query(project_ref, pat, sql)
    else:
        db_url = args.db_url or os.environ.get("SUPABASE_DB_URL")
        if not db_url:
            print("error: SUPABASE_DB_URL is required for direct DB mode.", file=sys.stderr)
            return 2
        rows = _run_direct(db_url, sql)

    print(json.dumps(rows, indent=2, default=str))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
