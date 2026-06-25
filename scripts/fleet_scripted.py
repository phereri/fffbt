#!/usr/bin/env python3
"""Run the scripted (no-agent) Trial-Reel poster across devices, in parallel.

Each device runs independently with a STAGGERED start. Per device it can post a
fixed COUNT of reels or LOOP continuously, pacing successful posts by a random
delay and never exceeding a rolling-24h per-account cap. Unbound devices are
auto-discovered (account read from the profile) before posting.

  * claims its own DB rows atomically (FOR UPDATE SKIP LOCKED — no duplicates),
  * writes its own per-device trajectory under trajectories/scripted/,
  * stops itself on a login challenge (BLOCKED) — never keeps tapping,
  * is fully independent — one device failing does not stop the others.

Usage:
  python scripts/fleet_scripted.py --devices <ip:5555> ... --stagger 20
  python scripts/fleet_scripted.py --devices ... --count 5 --delay-min 900 --delay-max 2700 --max-24h 20
  python scripts/fleet_scripted.py --devices ... --loop --delay-min 900 --delay-max 2700
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import selectors
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                       # scripts/  -> whoami
sys.path.insert(0, os.path.dirname(_HERE))      # repo root -> src, scripts.*

import account_identity as ai
from scripts.post_scripted import _drive
from scripts.post_trial import _load_env, _mgmt_query
from src.runner import fleet_events
from whoami import _open_profile, resolve_username

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
DEFAULT_DEVICES = [
    "192.168.5.46:5555",
    "192.168.5.141:5555",
    "192.168.5.143:5555",
]
_roster_lock = asyncio.Lock()


def _args_for(device: str, ns: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        device=device, category=ns.category, order=ns.order, url_ttl=ns.url_ttl,
        url_attempts=ns.url_attempts, url_retry_delay=ns.url_retry_delay,
        no_share=ns.no_share,
    )


def _count_24h(account: str) -> int | None:
    """This account's posts in the last 24h (posted/verify). None on query error."""
    try:
        r = _mgmt_query(
            "SELECT count(*) AS n FROM fffbt.videos WHERE posted_by="
            f"'{account}' AND status IN ('posted','verify') "
            "AND published_at > now() - interval '24 hours'")
        return int(r[0]["n"])
    except Exception:
        return None


def _mirror_account(user: str, device: str) -> None:
    """Best-effort: mirror a binding into automation.accounts (Phase-2 store) on a
    daemon thread so the slow Management-API call never blocks the launch loop."""
    def _go():
        try:
            import account_store
            account_store.bind_one(user, device)
        except Exception:
            pass
    try:
        import threading
        threading.Thread(target=_go, daemon=True).start()
    except Exception:
        pass


async def _ensure_bound(device: str) -> str | None:
    """If the device is unbound, discover the logged-in account from its profile
    and persist the binding (auto-discover). Returns the account or None."""
    acct = ai.account_for(device)
    if acct:
        return acct
    print(f"[{device}] unbound -> running discover account first")
    try:
        nodes = await _open_profile(device)
        user, _ = resolve_username(nodes)
    except Exception as e:
        print(f"[{device}] discover error: {e}")
        return None
    if not user or not ai.is_real_username(user):
        print(f"[{device}] discover: username unreadable / junk ({user!r}) -> skip")
        return None
    # MINT-SAFE bind: emit discover first (so this fresh read out-ranks any stale
    # twin), then atomically merge under the invariant. If `user` is already
    # canonical on a DIFFERENT serial, the write won't keep this device — refuse
    # rather than mint a duplicate.
    try:
        fleet_events.emit("discover", device=device, account=user)
    except Exception:
        pass
    ai.bust_events_cache()
    dropped: list = []

    def _mut(devices):
        devices[device] = user
        deduped, drp = ai.enforce_invariant(devices, reachable=ai._adb_online())
        dropped.extend(drp)
        return deduped

    try:
        async with _roster_lock:  # serialise within this process; atomic across procs
            written = await asyncio.to_thread(ai.atomic_update_roster, _mut)
    except OSError as e:
        print(f"[{device}] roster write busy ({e}) -> not binding this cycle")
        return None
    if written.get(device) != user:
        # the just-read live device did NOT win canon (an account already canonical
        # on another serial). ENFORCE: refuse the dup. OBSERVE (default): never break
        # a real post — this phone IS logged into `user`, so make it canonical (drop
        # the stale twin, keep injective) and record the conflict.
        if ai._enforce_default():
            print(f"[{device}] {user} canonical on another serial -> refusing dup (enforce), stopping")
            return None

        def _force(devices):
            for s in [s for s, a in list(devices.items()) if a == user and s != device]:
                del devices[s]
            devices[device] = user
            return devices

        try:
            async with _roster_lock:
                await asyncio.to_thread(ai.atomic_update_roster, _force)
        except OSError as e:
            print(f"[{device}] roster write busy ({e}) -> skip")
            return None
        try:
            fleet_events.emit("dup_observed", account=user, device=device,
                              reason="forced_canonical_observe", mode="observe")
        except Exception:
            pass
        _mirror_account(user, device)
        print(f"[{device}] OBSERVE: bound {user} -> {device} (was canonical elsewhere; stale twin dropped)")
        return user
    for acc, ds, keep, reason in dropped:
        try:
            fleet_events.emit("canon", account=acc, device=keep, dropped=ds, reason=reason)
        except Exception:
            pass
    _mirror_account(user, device)
    print(f"[{device}] discovered + bound -> {user}")
    return user


_STOP_FLAG = os.environ.get("FLEET_STOP_FLAG")


def _stop_requested() -> bool:
    """A graceful stop was requested by the dashboard (a flag file appeared)."""
    return bool(_STOP_FLAG) and os.path.exists(_STOP_FLAG)


async def _sleep_or_stop(seconds: float) -> bool:
    """Sleep, but wake early if a graceful stop is requested. Returns True if stopped."""
    end = time.monotonic() + seconds
    while True:
        if _stop_requested():
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(3.0, remaining))


async def _run_device(device: str, ns: argparse.Namespace, delay: float) -> tuple[str, int]:
    if delay > 0:
        print(f"[stagger] {device}: starting in {delay:.0f}s")
        if await _sleep_or_stop(delay):
            print(f"[{device}] stop requested during stagger — not starting")
            try:
                fleet_events.emit("device_done", device=device, rc=1, posted=0, reason="stopped")
            except Exception:
                pass
            return device, 1
    print(f"========== START {device} ==========")

    account = await _ensure_bound(device)
    if not account:
        print(f"========== END {device} rc=1 (no account) ==========")
        # release this device from the task so it can be reassigned elsewhere
        try:
            fleet_events.emit("device_done", device=device, rc=1, posted=0, reason="no_account")
        except Exception:
            pass
        return device, 1

    # --- ACCOUNT DEDUP GUARD (observe by default; FLEET_DEDUP_ENFORCE=1 = block) -
    # The per-account lock is held by THIS long-lived loop owner across the
    # inter-post sleeps, so a second device can never post for the same account
    # in the gap. assert/validate are observe-or-enforce per the env flag.
    try:
        ai.assert_can_launch(device, account)
    except ai.DuplicateAccount as e:
        print(f"[{device}] DUP_BLOCKED {account}: canonical={e.canonical} ({e.reason})")
        try:
            fleet_events.emit("device_done", account=account, device=device,
                              rc=9, last_rc=9, posted=0, reason="dup_blocked")
        except Exception:
            pass
        return device, 9
    _acct_lock = ai.account_lock(account)
    try:
        _acct_lock.__enter__()
    except ai.DuplicateAccount:
        print(f"[{device}] DUP_BLOCKED {account}: lock held by a live runner")
        try:
            fleet_events.emit("device_done", account=account, device=device,
                              rc=9, last_rc=9, posted=0, reason="dup_locked")
        except Exception:
            pass
        return device, 9
    try:
        ai.validate_canon_under_lock(device, account)
    except ai.DuplicateAccount as e:
        _acct_lock.__exit__(None, None, None)
        print(f"[{device}] DUP_BLOCKED {account}: canon flipped to {e.canonical}")
        try:
            fleet_events.emit("device_done", account=account, device=device,
                              rc=9, last_rc=9, posted=0, reason="dup_canon_flipped")
        except Exception:
            pass
        return device, 9

    target = float("inf") if ns.loop else max(1, ns.count)
    posted, fails, last_rc = 0, 0, 1
    t0 = time.monotonic()
    while posted < target:
        # ACCOUNT SWAP: the operator logged a NEW account onto this phone mid-run
        # (login-challenge swap). Stop before stamping posted_by with the stale
        # account; the device re-binds + relaunches under the new one.
        _cur = ai.account_for(device)
        if _cur is not None and _cur != account:
            print(f"[{device}] account swapped {account} -> {_cur} mid-run — stopping")
            last_rc = 10
            break
        # GRACEFUL STOP: a stop was requested -> claim no more videos and end the loop.
        # Any post already in flight (incl. verify) finishes; the dashboard unclaims
        # anything left un-posted afterwards.
        if _stop_requested():
            print(f"[{device}] graceful stop — no more posts")
            break
        # never exceed the rolling-24h cap
        if ns.max_24h > 0:
            n = await asyncio.to_thread(_count_24h, account)
            if n is not None and n >= ns.max_24h:
                if ns.loop:
                    print(f"[{device}] {account} at 24h cap {n}/{ns.max_24h} — waiting 15m")
                    until = (datetime.now(timezone.utc)
                             + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    fleet_events.emit("rate_limit", account=account, device=device,
                                      count=n, cap=ns.max_24h, until=until)
                    if await _sleep_or_stop(15 * 60):
                        break
                    continue
                print(f"[{device}] {account} at 24h cap {n}/{ns.max_24h} — stopping")
                break
        try:
            rc = await _drive(_args_for(device, ns))
        except Exception as e:
            print(f"[{device}] run error: {e}")
            rc = 1
        last_rc = rc
        if rc in (0, 2):                      # posted (verified or unconfirmed)
            posted += 1
            fails = 0
            if posted < target:
                d = random.uniform(ns.delay_min, ns.delay_max)
                until = (datetime.now(timezone.utc)
                         + timedelta(seconds=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
                print(f"[{device}] posted {posted}"
                      f"{('/' + str(ns.count)) if not ns.loop else ' (loop)'}; next in {d / 60:.0f}m")
                # cooldown until the next post — surfaced as a live countdown
                fleet_events.emit("sleep", account=account, device=device,
                                  seconds=int(d), until=until)
                if await _sleep_or_stop(d):          # wake at once on a graceful stop
                    break
        elif rc == 4:                         # BLOCKED (login challenge)
            print(f"[{device}] BLOCKED (login challenge) — stopping device")
            break
        elif rc == 6:                         # trial reels not enabled on this account
            print(f"[{device}] TRIAL_UNAVAILABLE (trial reels not enabled) — stopping device")
            break
        elif rc == 8:                         # IG trial-reels rate limit reached
            print(f"[{device}] TRIAL_LIMIT (max trial reels reached) — stopping device")
            break
        elif rc == 7:                         # proxy down / no connectivity
            print(f"[{device}] PROXY_DOWN (proxy not working) — stopping device")
            break
        elif rc == 3:                         # no rows to claim
            print(f"[{device}] no videos available — stopping")
            break
        elif rc == 11:                        # caption uniquifier down — don't post non-unique
            print(f"[{device}] UNIQUIFY_DOWN (caption uniquifier unavailable) — stopping device")
            break
        else:                                 # transient failure -> short retry
            fails += 1
            if fails >= 5:
                print(f"[{device}] 5 consecutive failures — stopping")
                break
            if await _sleep_or_stop(90):
                break

    _acct_lock.__exit__(None, None, None)   # release the per-account lock
    rc = 0 if posted else last_rc
    if last_rc == 10:
        rc = 10                              # ACCOUNT_SWAPPED is terminal, not success
    print(f"========== END {device} rc={rc} posted={posted} ({time.monotonic() - t0:.0f}s) ==========")
    # this device's loop has ended (done / blocked / trial-unavailable / no-rows /
    # too many fails) -> release it from the task so it's free for other work. The
    # task itself ends when ALL its devices have finished (the process exits).
    # `rc` collapses to 0 when the device posted at least once (task-summary view);
    # `last_rc` preserves the REAL terminal code of the final attempt (e.g. 8
    # TRIAL_LIMIT) so the dashboard can show why the loop actually stopped.
    try:
        fleet_events.emit("device_done", account=account, device=device,
                          rc=rc, last_rc=last_rc, posted=posted)
    except Exception:
        pass
    return device, rc


async def _main_async(ns: argparse.Namespace) -> int:
    devices = ns.devices or DEFAULT_DEVICES
    mode = "loop" if ns.loop else f"count={ns.count}"
    print(f"fleet_scripted: {len(devices)} devices, stagger={ns.stagger}s, {mode}, "
          f"delay={ns.delay_min}-{ns.delay_max}s, max_24h={ns.max_24h}, "
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
        verdict = {0: "SUCCESS", 2: "PUBLISHED_UNCONFIRMED", 3: "NO_ROWS",
                   4: "BLOCKED (login challenge)", 5: "A11Y_DOWN",
                   6: "TRIAL_UNAVAILABLE", 7: "PROXY_DOWN", 8: "TRIAL_LIMIT",
                   9: "DUP_BLOCKED", 10: "ACCOUNT_SWAPPED", 11: "UNIQUIFY_DOWN"}.get(rc, "FAILED")
        print(f"  {device:24} rc={rc}  {verdict}")
        worst = max(worst, 0 if rc in (0, 3) else rc)
    return worst


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet_scripted")
    p.add_argument("--devices", nargs="*", default=None,
                   help=f"adb serials (default: {' '.join(DEFAULT_DEVICES)})")
    p.add_argument("--category", default="trend")
    p.add_argument("--order", choices=("asc", "desc"), default="asc",
                   help="claim oldest-first (asc) or newest-first (desc)")
    p.add_argument("--stagger", type=float, default=20.0, help="seconds between device starts")
    p.add_argument("--count", type=int, default=1, help="reels to post per device")
    p.add_argument("--loop", action="store_true", help="post continuously (overrides --count)")
    p.add_argument("--delay-min", type=int, default=900, help="min seconds between successful posts")
    p.add_argument("--delay-max", type=int, default=2700, help="max seconds between successful posts")
    p.add_argument("--max-24h", type=int, default=20, help="rolling-24h per-account cap (0 = off)")
    p.add_argument("--url-ttl", type=int, default=3600)
    p.add_argument("--url-attempts", type=int, default=4)
    p.add_argument("--url-retry-delay", type=int, default=30)
    p.add_argument("--no-share", action="store_true", help="dry-run all devices (no publish)")
    return p


def main(argv: list[str] | None = None) -> int:
    _load_env()
    ns = _build_parser().parse_args(argv)
    if ns.delay_max < ns.delay_min:
        ns.delay_max = ns.delay_min
    if sys.platform == "win32":
        return int(asyncio.run(_main_async(ns),
                               loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    return int(asyncio.run(_main_async(ns)))


if __name__ == "__main__":
    raise SystemExit(main())
