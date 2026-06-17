"""Per-account learned preferences — MVP local store.

Records, per Instagram account, two self-learned navigation choices so the next
run can try the known-good route FIRST instead of probing every option:

  * ``trial_reels_path`` — which Trial Reels ENTRY path (A/B/C) reached the
    composer (where to POST). Shortens navigation for accounts whose dashboard
    layout makes an early path unavailable.
  * ``verify_path`` — which route CONFIRMED the post is live (where to VERIFY):
    ``"reels"`` (deterministic Profile→Reels→Trial-reels capture, also yields
    the link) or ``"dashboard"`` (LLM Professional-dashboard check). Lets the
    next run skip the slower/flakier route once one is known to work.

MVP: stored locally as JSON (``data/account_memory.json``, gitignored).
Production (future, push to git): move into the DB per-account so the learned
choices are shared across hosts. See ``docs/standalone-trial-poster.md`` (C2).

All functions are best-effort and never raise — a memory miss must not break a
post.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("post_trial")

_VALID_PATHS = ("A", "B", "C")
_VALID_VERIFY = ("reels", "dashboard")
_DEFAULT_PATH = os.environ.get("ACCOUNT_MEMORY_PATH", "data/account_memory.json")


def _load(store_path: str) -> dict:
    p = Path(store_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("account_memory: could not read %s: %s", store_path, e)
        return {}


def get_preferred_path(account: str | None, *, store_path: str = _DEFAULT_PATH) -> str | None:
    """Return the learned Trial Reels path (A/B/C) for ``account``, or None."""
    if not account:
        return None
    rec = _load(store_path).get(account.lower())
    if isinstance(rec, dict):
        value = rec.get("trial_reels_path")
        if value in _VALID_PATHS:
            return value
    return None


def get_preferred_verify_path(account: str | None, *, store_path: str = _DEFAULT_PATH) -> str | None:
    """Return the learned verification route ('reels'/'dashboard') for ``account``."""
    if not account:
        return None
    rec = _load(store_path).get(account.lower())
    if isinstance(rec, dict):
        value = rec.get("verify_path")
        if value in _VALID_VERIFY:
            return value
    return None


def _record_field(
    account: str | None,
    field: str,
    value: str | None,
    valid: tuple[str, ...],
    *,
    store_path: str,
    timestamp: str | None,
) -> None:
    if not account or value not in valid:
        return
    data = _load(store_path)
    rec = data.get(account.lower())
    if not isinstance(rec, dict):
        rec = {}
    rec[field] = value
    if timestamp:
        rec["updated_at"] = timestamp
    data[account.lower()] = rec
    p = Path(store_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("account_memory: recorded %s=%s for %s", field, value, account)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("account_memory: could not write %s: %s", store_path, e)


def record_path(
    account: str | None,
    trial_path: str | None,
    *,
    store_path: str = _DEFAULT_PATH,
    timestamp: str | None = None,
) -> None:
    """Persist the Trial Reels entry path that worked for ``account`` (best-effort)."""
    _record_field(account, "trial_reels_path", trial_path, _VALID_PATHS,
                  store_path=store_path, timestamp=timestamp)


def record_verify_path(
    account: str | None,
    verify_path: str | None,
    *,
    store_path: str = _DEFAULT_PATH,
    timestamp: str | None = None,
) -> None:
    """Persist the verification route that confirmed the post for ``account``."""
    _record_field(account, "verify_path", verify_path, _VALID_VERIFY,
                  store_path=store_path, timestamp=timestamp)


__all__ = [
    "get_preferred_path",
    "record_path",
    "get_preferred_verify_path",
    "record_verify_path",
]
