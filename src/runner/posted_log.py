"""Append-only record of published Trial Reels.

Every successful (or attempted) post is written as one JSON object per line to a
JSONL file (default ``posted_reels.jsonl``). JSONL is chosen over a single JSON
array because it is append-safe — concurrent / repeated runs just add a line,
the file never has to be rewritten, and it stays both human-greppable and
machine-readable.

Each line records *which video from the bucket* was posted, *its category*, and
*the resulting link* — plus enough context (device, account, caption, time,
status) to later drive the "posted to every required platform → delete from S3"
step.

Example line::

    {"ts": "2026-06-14T09:30:00Z", "platform": "instagram", "status": "published",
     "video_id": "Cowboy", "category": "trend", "source_key": "ferma/Cowboy/VID_x.mp4",
     "post_url": "https://www.instagram.com/reel/ABC/", "device": "100.x:5555",
     "account": "uctamdoan.83862", "caption": "…", "verified": true}
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = "posted_reels.jsonl"


def _utc_now_iso() -> str:
    # Imported lazily-friendly: datetime is cheap and always available.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PostedRecord:
    """One published-reel log line."""

    ts: str
    platform: str  # "instagram"
    status: str  # "published" | "published_unverified" | "failed"
    video_id: str | None = None  # bucket folder, e.g. "Cowboy"
    category: str | None = None  # from meta.json
    source_key: str | None = None  # full S3 key of the exact video file
    source_video: str | None = None  # the --video value as given (path or url)
    post_url: str | None = None  # best-effort reel link (may be null)
    device: str | None = None
    account: str | None = None
    caption: str | None = None
    verified: bool | None = None
    code: str | None = None  # failure/needs-review code, if any
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(cls, *, platform: str = "instagram", **kw: Any) -> "PostedRecord":
        return cls(ts=_utc_now_iso(), platform=platform, **kw)


def append_record(record: PostedRecord, path: str | Path | None = None) -> Path:
    """Append one record as a JSON line. Creates the file/dirs if missing.

    The log path comes from (in order): explicit ``path`` arg,
    ``POSTED_REELS_LOG`` env var, then ``DEFAULT_LOG_PATH``.
    """
    target = Path(
        path or os.environ.get("POSTED_REELS_LOG") or DEFAULT_LOG_PATH
    )
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_clean(asdict(record)), ensure_ascii=False)
    with open(target, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    logger.info("posted_log: wrote %s -> %s", record.status, target)
    return target


def read_records(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read all records back (skips blank/corrupt lines). Mostly for tests/tools."""
    target = Path(
        path or os.environ.get("POSTED_REELS_LOG") or DEFAULT_LOG_PATH
    )
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("posted_log: skipping corrupt line in %s", target)
    return out


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Drop empty ``extra`` so lines stay tidy; keep explicit None fields."""
    if not d.get("extra"):
        d.pop("extra", None)
    return d


__all__ = ["PostedRecord", "append_record", "read_records", "DEFAULT_LOG_PATH"]
