#!/usr/bin/env python3
"""Run the scripted (no-agent) Trial-Reel poster on several devices in parallel.

Launches scripts/post_scripted._drive concurrently on each device with a small
STAGGERED start (default 20s apart) so they don't all hit the gallery / S3 /
share UI at the exact same instant. Each device:
  * claims its own DB row atomically (FOR UPDATE SKIP LOCKED — no duplicates),
  * writes its own per-device trajectory under trajectories/scripted/<ts>_<ip>/,
  * is fully independent — one device failing does not stop the others.

Usage:
  python scripts/fleet_scripted.py
  python scripts/fleet_scripted.py --devices 192.168.5.46:5555 192.168.5.141:5555 ...
  python scripts/fleet_scripted.py --stagger 20 --no-share
"""
from __future__ import annotations

import argparse
import asyncio
import os
import selectors
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.post_scripted import _drive
from scripts.post_trial import _load_env

DEFAULT_DEVICES = [
    "192.168.5.46:5555",
    "192.168.5.141:5555",
    "192.168.5.143:5555",
]


def _args_for(device: str, ns: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        category=ns.category,
        url_ttl=ns.url_ttl,
        url_attempts=ns.url_attempts,
        url_retry_delay=ns.url_retry_delay,
        no_share=ns.no_share,
    )


async def _run_device(device: str, ns: argparse.Namespace, delay: float) -> tuple[str, int]:
    if delay > 0:
        print(f"[stagger] {device}: starting in {delay:.0f}s")
        await asyncio.sleep(delay)
    print(f"========== START {device} ==========")
    t0 = time.monotonic()
    try:
        rc = await _drive(_args_for(device, ns))
    except Exception as e:  # one device must never take down the fleet
        print(f"[{device}] CRASHED: {e}")
        rc = 1
    print(f"========== END {device} rc={rc} ({time.monotonic() - t0:.0f}s) ==========")
    return device, rc


async def _main_async(ns: argparse.Namespace) -> int:
    devices = ns.devices or DEFAULT_DEVICES
    print(f"fleet_scripted: {len(devices)} devices, stagger={ns.stagger}s, "
          f"category={ns.category}, no_share={ns.no_share}")
    tasks = [_run_device(d, ns, i * ns.stagger) for i, d in enumerate(devices)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    print("\n================ FLEET SUMMARY ================")
    worst = 0
    for r in results:
        if isinstance(r, Exception):
            print(f"  (task error) {r}")
            worst = max(worst, 1)
            continue
        device, rc = r
        verdict = {0: "SUCCESS", 2: "PUBLISHED_UNCONFIRMED", 3: "NO_ROWS"}.get(rc, "FAILED")
        print(f"  {device:24} rc={rc}  {verdict}")
        worst = max(worst, 0 if rc in (0, 3) else rc)
    return worst


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet_scripted")
    p.add_argument("--devices", nargs="*", default=None,
                   help=f"adb serials (default: {' '.join(DEFAULT_DEVICES)})")
    p.add_argument("--category", default="trend")
    p.add_argument("--stagger", type=float, default=20.0, help="seconds between device starts")
    p.add_argument("--url-ttl", type=int, default=3600)
    p.add_argument("--url-attempts", type=int, default=4)
    p.add_argument("--url-retry-delay", type=int, default=30)
    p.add_argument("--no-share", action="store_true", help="dry-run all devices (no publish)")
    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    ns = _build_parser().parse_args(argv)
    if sys.platform == "win32":
        return int(asyncio.run(_main_async(ns),
                               loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    return int(asyncio.run(_main_async(ns)))


if __name__ == "__main__":
    raise SystemExit(main())
