"""Structured event stream for the fleet — feeds the local dashboard.

The proven posting pipeline already prints rich per-stage timings
(``post_trial._print_report``) and grep-able status lines
(``post_loop``), but that data is *thrown away*: ``post_loop.post_once``
captures ``post_trial``'s stdout only to scrape ``code`` out of it. This
module gives every fleet process one shared, append-only JSON-lines file so
the dashboard can reconstruct, per account/device:

  * the current stage (claim → prepare → publish → verify → result),
  * how long each video and each of its stages took, and
  * the loop / supervisor lifecycle (sleep, recover, escalate, stop).

One event == one line of JSON in ``data/fleet_events.jsonl`` (override with
``FLEET_EVENTS``). Writers append a single ``f.write`` per event (atomic
enough for the handful of fleet processes), readers skip any malformed /
half-written trailing line. Best-effort: emitting must NEVER break a post.

Event ``type`` values used today:
  fleet_start, fleet_spawned, fleet_child_exit, fleet_stop,     (post_fleet)
  loop_start, sleep, rate_limit, recover, escalate, loop_stop,  (post_loop)
  claim, stage_start, stage_done, published, result             (post_trial)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("fleet_events")

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = os.environ.get("FLEET_EVENTS", str(_ROOT / "data" / "fleet_events.jsonl"))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(
    event: str,
    *,
    account: str | None = None,
    device: str | None = None,
    path: str | None = None,
    **fields,
) -> None:
    """Append one event line. Best-effort — never raises.

    ``fields`` are merged into the record verbatim (timings, video_id, etc.).
    """
    store = path or _DEFAULT_PATH
    rec = {"ts": now_iso(), "type": event}
    if account is not None:
        rec["account"] = account
    if device is not None:
        rec["device"] = device
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        p = Path(store)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:  # pragma: no cover - telemetry must not break a post
        logger.debug("fleet_events: could not write event %s: %s", event, e)


def read_events(*, path: str | None = None, since_ts: str | None = None) -> list[dict]:
    """Read all events (optionally only those at/after ``since_ts``).

    Skips malformed lines so a half-written trailing record never crashes a
    reader. Returns events in file (chronological) order.
    """
    store = path or _DEFAULT_PATH
    p = Path(store)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            if since_ts and rec.get("ts", "") < since_ts:
                continue
            out.append(rec)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("fleet_events: could not read %s: %s", store, e)
    return out


__all__ = ["emit", "read_events", "now_iso"]
