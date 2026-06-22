"""One-way S3 -> ``fffbt.videos`` sync (insert-only).

Mirrors every video object under the Ferma S3 prefix into the Supabase
``fffbt.videos`` table:

  * a new object in S3 becomes a new row on the next pass, and
  * an object deleted from S3 is **never** removed from the DB.

Insert-only is exactly that contract: the sync only ever adds rows, so a
deletion in the bucket simply stops producing a candidate — the existing row
is left untouched.

Granularity is one row per (video file x platform listed in the folder's
``meta.json``). Idempotency is by ``(link_drive, platform)``: a candidate whose
``s3://`` URI + platform already exist in the DB is skipped. The surrogate text
``id`` is freshly generated (28-hex, matching the existing S3-era id format in
``fffbt.videos``) because it is not derivable from the object.

The pure helpers (``build_candidates``, ``insert_sql``) are unit-tested on
fakes; the network/DB pieces (``FermaS3``, the Supabase Management API) are
injected into ``sync_once`` so tests need neither boto3 nor a database.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable

from src.runner.s3_source import FermaS3, VideoFolder

logger = logging.getLogger(__name__)

# Columns we populate explicitly; everything else (created_at/updated_at,
# link_platform, posted_by, published_at, views) takes its DB default / NULL.
_COLUMNS = ("id", "name", "platform", "category", "type", "status", "link_drive", "caption")

_INSERT_CHUNK = 500

# Skip videos whose VID_YYYYMMDD filename date is older than this many days. The
# dashboard daemon passes this; 0 / unset disables the gate. Keeps stale objects
# lingering in the bucket out of the postable backlog.
MAX_AGE_DAYS = int(os.environ.get("S3_SYNC_MAX_AGE_DAYS", "7") or "0")

_DATE_RE = re.compile(r"(\d{8})")


def _name_date(name: str | None) -> date | None:
    """First 8-digit run in a filename ('VID_20260616_..' -> 2026-06-16), or None."""
    m = _DATE_RE.search(name or "")
    if not m:
        return None
    s = m.group(1)
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


@dataclass
class SyncResult:
    """Outcome of one ``sync_once`` pass."""

    folders: int = 0
    folders_skipped: int = 0          # folders with no usable meta.json
    candidates: int = 0
    inserted: int = 0
    skipped: int = 0                  # candidates already present in the DB
    skipped_old: int = 0              # candidates dropped by the age gate
    skipped_folder_ids: list[str] = field(default_factory=list)


def _lit(value: str | None) -> str:
    """SQL literal: escape single quotes, ``None`` -> ``NULL``."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _new_id() -> str:
    # 28 lowercase hex chars — same shape as the existing S3-era ids.
    return secrets.token_hex(14)


def _basename(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def build_candidates(
    folders: Iterable[VideoFolder],
    bucket: str,
    *,
    id_factory: Callable[[], str] = _new_id,
) -> tuple[list[dict], list[str]]:
    """Expand folders into candidate rows (one per video x platform).

    A folder is skipped (its ``video_id`` returned in the second list) when it
    has no ``meta.json``, no ``category``, or an empty ``platform`` list — such
    rows could neither satisfy the NOT NULL columns nor be claimed by the
    poster, so inserting them would be noise.
    """
    rows: list[dict] = []
    skipped: list[str] = []
    for folder in folders:
        meta = folder.meta
        if meta is None or not meta.category or not meta.platform:
            skipped.append(folder.video_id)
            continue
        for key in folder.video_keys:
            link = f"s3://{bucket}/{key}"
            for platform in meta.platform:
                rows.append(
                    {
                        "id": id_factory(),
                        "name": _basename(key),
                        "platform": platform,
                        "category": meta.category,
                        "type": "",
                        "status": "new",
                        "link_drive": link,
                        "caption": meta.caption,
                    }
                )
    return rows, skipped


def insert_sql(rows: list[dict]) -> str:
    """Build a multi-row ``INSERT INTO fffbt.videos`` for the Management API."""
    cols = ", ".join(_COLUMNS)
    values = ",\n  ".join(
        "(" + ", ".join(_lit(r[c]) for c in _COLUMNS) + ")" for r in rows
    )
    return f"INSERT INTO fffbt.videos ({cols}) VALUES\n  {values};"


# ---------------------------------------------------------------------------
# Supabase Management API (self-contained, mirrors scripts/post_trial.py)
# ---------------------------------------------------------------------------
def _mgmt_query(sql: str) -> list[dict]:
    ref = os.environ["SUPABASE_PROJECT_REF"]
    pat = os.environ["SUPABASE_PAT"]
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "User-Agent": "fffbt-s3-sync/1.0",
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


def fetch_existing_pairs() -> set[tuple[str, str]]:
    """``(link_drive, platform)`` for every S3-sourced row already in the DB."""
    rows = _mgmt_query(
        "SELECT link_drive, platform FROM fffbt.videos WHERE link_drive LIKE 's3://%'"
    )
    return {(r["link_drive"], r["platform"]) for r in rows}


def insert_rows(rows: list[dict]) -> int:
    """Insert candidate rows in chunks. Returns the number inserted."""
    inserted = 0
    for i in range(0, len(rows), _INSERT_CHUNK):
        chunk = rows[i : i + _INSERT_CHUNK]
        if not chunk:
            continue
        _mgmt_query(insert_sql(chunk))
        inserted += len(chunk)
    return inserted


def sync_once(
    *,
    s3: FermaS3 | None = None,
    fetch_existing: Callable[[], set[tuple[str, str]]] = fetch_existing_pairs,
    insert: Callable[[list[dict]], int] = insert_rows,
    id_factory: Callable[[], str] = _new_id,
    max_age_days: int | None = None,
    today: date | None = None,
) -> SyncResult:
    """Run one S3 -> DB pass. Insert-only; never deletes.

    ``s3``, ``fetch_existing`` and ``insert`` are injectable so this can be
    exercised without boto3 or a live database. When ``max_age_days`` is a
    positive int, candidates whose filename date is older than that many days
    (relative to ``today``, default the real today) are dropped before insert;
    candidates with no parseable date are treated as too old and dropped too.
    ``max_age_days=None`` (default) disables the gate.
    """
    s3 = s3 or FermaS3.from_env()
    folders = [s3.get_folder(name) for name in s3.list_folders()]
    candidates, skipped_folders = build_candidates(
        folders, s3.config.bucket, id_factory=id_factory
    )

    skipped_old = 0
    if max_age_days and max_age_days > 0:
        cutoff = (today or date.today()) - timedelta(days=int(max_age_days))
        fresh: list[dict] = []
        for row in candidates:
            d = _name_date(row.get("name"))
            if d is None or d < cutoff:
                skipped_old += 1
                continue
            fresh.append(row)
        candidates = fresh

    existing = fetch_existing()
    seen: set[tuple[str, str]] = set()
    new_rows: list[dict] = []
    for row in candidates:
        pair = (row["link_drive"], row["platform"])
        if pair in existing or pair in seen:  # dedup vs DB and within this pass
            continue
        seen.add(pair)
        new_rows.append(row)

    inserted = insert(new_rows)
    return SyncResult(
        folders=len(folders),
        folders_skipped=len(skipped_folders),
        candidates=len(candidates),
        inserted=inserted,
        skipped=len(candidates) - len(new_rows),
        skipped_old=skipped_old,
        skipped_folder_ids=skipped_folders,
    )


__all__ = [
    "SyncResult",
    "build_candidates",
    "insert_sql",
    "sync_once",
    "fetch_existing_pairs",
    "insert_rows",
]
