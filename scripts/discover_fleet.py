#!/usr/bin/env python3
"""Discover the logged-in Instagram account on each device, write the binding.

Runs the whoami profile-read (deterministic adb — open IG, tap Profile, read the
action-bar username) concurrently across all serials, then writes
``data/device_accounts.json``. Read-only on each device; no posting, no LLM.

Usage:
  python scripts/discover_fleet.py 192.168.5.11:5555 192.168.5.14:5555 ...
"""
from __future__ import annotations

import asyncio
import json
import os
import selectors
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whoami import _open_profile, resolve_username  # noqa: E402

from src.runner import fleet_events  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
CONCURRENCY = int(os.environ.get("DISCOVER_CONCURRENCY", "8"))


async def _one(serial: str, sem: asyncio.Semaphore) -> tuple[str, str | None]:
    async with sem:
        try:
            nodes = await _open_profile(serial)
            user, _rid = resolve_username(nodes)
            print(f"  {serial:>22} -> {user or '<unreadable>'}", flush=True)
            return serial, user
        except Exception as e:  # pragma: no cover - per-device best-effort
            print(f"  {serial:>22} -> ERROR {e!r}", flush=True)
            return serial, None


async def main() -> int:
    replace = "--replace" in sys.argv
    raw = [s for s in sys.argv[1:] if not s.startswith("--")]
    serials = [s if ":" in s else f"{s}:5555" for s in raw]
    if not serials:
        print("usage: discover_fleet.py <serial> [<serial> ...] [--replace]")
        return 2
    print(f"discovering {len(serials)} devices (concurrency {CONCURRENCY}, "
          f"{'replace' if replace else 'merge'})…")
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[_one(s, sem) for s in serials])

    # MERGE into the existing roster by default: only the serials that resolved
    # are updated; every other binding is preserved — so discovering ONE new
    # device never clobbers the rest. --replace forces a fresh file from only this
    # run's serials. (There is no "disabled" device state — devices are selected
    # manually in the dashboard.)
    existing: dict = {}
    if not replace and BINDING.exists():
        try:
            existing = json.loads(BINDING.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    merged = dict(existing.get("devices") or {})
    added, changed = [], []
    for s, u in results:
        if not u:
            continue
        if s not in merged:
            added.append(s)
        elif merged[s] != u:
            changed.append((s, merged[s], u))
        merged[s] = u

    unreadable = [s for s, u in results if not u]
    dupes = {u: sorted(d for d, uu in merged.items() if uu == u)
             for u in set(merged.values())
             if sum(1 for uu in merged.values() if uu == u) > 1}

    payload: dict = {
        "_comment": "Account<->device binding for the fleet launcher. serial -> IG "
                    "username, discovered live via scripts/discover_fleet.py "
                    "(read from each device's profile action_bar_title). MVP local "
                    "store; gitignored. IPs rotate on router reboot — re-run after.",
        "devices": merged,
    }
    # preserve any operator-curated underscore keys across a merge (e.g. _comment)
    for k, v in (existing or {}).items():
        if k.startswith("_") and k not in ("_comment", "_unreadable", "_duplicate_accounts"):
            payload[k] = v
    if unreadable:
        payload["_unreadable"] = unreadable
    if dupes:
        payload["_duplicate_accounts"] = dupes
    BINDING.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # Announce each (re)binding on the fleet-events stream. A serial now bound to a
    # readable (working) account clears any prior BLOCKED flag the dashboard showed
    # for it — discovering a device after the operator swaps in a fresh account
    # flips it from blocked back to normal.
    for s, u in results:
        if u:
            try:
                fleet_events.emit("discover", device=s, account=u)
            except Exception:
                pass

    print(f"\nroster now {len(merged)} bindings ({len(added)} new, {len(changed)} changed) -> {BINDING}")
    for s, old, new in changed:
        print(f"  CHANGED {s}: {old} -> {new}")
    if unreadable:
        print(f"UNREADABLE ({len(unreadable)}): {unreadable}")
    if dupes:
        print(f"DUPLICATE accounts on >1 device (review!): {dupes}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
