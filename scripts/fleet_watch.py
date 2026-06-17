#!/usr/bin/env python3
"""Block until a fleet device hits its error limit, then exit so the operator
(and the assistant supervising this background task) is pulled in.

Each ``post_loop`` child stops itself on escalation (5 consecutive failures,
an account hard-stop, a reboot-cap hit, or no videos left) and emits a
``{"type":"escalate", ...}`` event. The other devices keep running, so the
fleet process does NOT exit — without this watcher an escalation would go
unnoticed until the whole fleet ended. This watcher polls the event stream and
exits the moment it sees an escalation after ``--since``, printing it, so the
failure can be resolved together instead of silently dropping a device.

Exit codes:
  10 — one or more devices escalated (details printed; resolve with operator)
   0 — the whole fleet stopped cleanly (``fleet_stop``) with no escalation

Usage:
  python scripts/fleet_watch.py --since 2026-06-16T23:00:00Z [--poll 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.runner import fleet_events  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(prog="fleet_watch")
    ap.add_argument("--since", required=True, help="ISO ts; only react to events at/after this.")
    ap.add_argument("--events", default=None, help="events jsonl path (default FLEET_EVENTS).")
    ap.add_argument("--poll", type=int, default=20, help="seconds between polls.")
    args = ap.parse_args()

    print(f"fleet_watch: watching for escalations since {args.since} "
          f"(poll {args.poll}s)", flush=True)
    while True:
        events = fleet_events.read_events(path=args.events, since_ts=args.since)
        escalations = [e for e in events if e.get("type") == "escalate"]
        if escalations:
            print("ESCALATION DETECTED — fleet device hit its error limit:", flush=True)
            for e in escalations:
                print("  " + json.dumps(e, ensure_ascii=False), flush=True)
            # context: the last few post_fail events per escalated device
            bad_devices = {e.get("device") for e in escalations}
            fails = [e for e in events if e.get("type") == "post_fail"
                     and e.get("device") in bad_devices]
            if fails:
                print("recent post_fail events for those devices:", flush=True)
                for e in fails[-12:]:
                    print("  " + json.dumps(e, ensure_ascii=False), flush=True)
            return 10
        # clean end of the whole fleet, no escalation seen → nothing to resolve
        if any(e.get("type") == "fleet_stop" for e in events):
            print("fleet_watch: fleet stopped cleanly, no escalation.", flush=True)
            return 0
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
