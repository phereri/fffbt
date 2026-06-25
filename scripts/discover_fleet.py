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
from src.worker.tools._adb import shell  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
CONCURRENCY = int(os.environ.get("DISCOVER_CONCURRENCY", "8"))
_A11Y_URI = "content://com.mobilerun.portal/state"


def _blocked_serials() -> set:
    """Serials whose latest posting outcome was a login challenge (BLOCKED), not yet
    cleared by a later successful read/post. The bind PRESERVES these: a blocked
    account is VALID (just challenged) — its profile screen can't be read, so it
    must NEVER be pruned or no-account-cleared. (read_events is chronological, so the
    last relevant event per device wins.)"""
    state: dict = {}
    try:
        for e in fleet_events.read_events():
            dev = e.get("device")
            if not dev:
                continue
            t = e.get("type")
            if t == "result":
                state[dev] = (e.get("verdict") == "BLOCKED")
            elif t in ("discover", "published"):   # a clean read/post => not blocked
                state[dev] = False
    except Exception:
        pass
    return {s for s, blk in state.items() if blk}


async def _ig_installed(serial: str) -> bool:
    """True if Instagram is installed. The no-account auto-clear fires ONLY when IG
    is genuinely MISSING (an old IP reassigned to a blank phone after rotation) — a
    reachable device WITH IG that is merely unreadable (login challenge / logged out
    / transient nav or adb glitch) keeps its binding. On any error we assume present
    (never clear on uncertainty)."""
    try:
        raw = await shell(serial, "pm path com.instagram.android", timeout=12)
    except Exception:
        return True
    return "package:" in (raw or "")


async def _a11y_state(serial: str) -> str:
    """'down' if the Mobilerun a11y provider isn't serving a tree (service off /
    provider not registered), 'up' if it returns a tree, 'unknown' on adb error.
    Distinguishes a recoverable a11y drop from a genuinely account-less device."""
    try:
        raw = await shell(serial, f"content query --uri {_A11Y_URI}", timeout=12)
    except Exception:
        return "unknown"
    if not raw:
        return "down"
    compact = raw.replace(" ", "")
    if "a11y_tree" in raw and '"status":"success"' in compact:
        return "up"
    low = raw.lower()
    if ("not available" in low or "could not find provider" in low
            or '"status":"error"' in compact):
        return "down"
    return "down"


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
    import account_identity as ai
    replace = "--replace" in sys.argv
    dry_run = "--dry-run" in sys.argv
    raw = [s for s in sys.argv[1:] if not s.startswith("--")]
    # normalize + DEDUP (bare host and host:5555 must not double-count in the guard)
    serials = list(dict.fromkeys(s if ":" in s else f"{s}:5555" for s in raw))
    if not serials:
        print("usage: discover_fleet.py <serial> [<serial> ...] [--replace] [--dry-run]")
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

    # PRUNE STALE BINDINGS (IP-rotation fix): a device we were asked to bind that
    # ALREADY had an account but now reads UNREADABLE has most likely lost that
    # account to a new IP. Drop its stale binding so the old-IP entry can't survive
    # as a duplicate of the account's new device. A transient read failure simply
    # re-binds on the next successful discover.
    prev_devices = existing.get("devices") or {}
    failed_bound = [s for s in unreadable if s in prev_devices]
    prev_bound = len(prev_devices)
    reachable = ai._adb_online() or set()
    # BLOCKED devices (login challenge) are PRESERVED untouched: the account is valid,
    # only its profile screen can't be read — never prune or clear them.
    blocked = _blocked_serials()
    # Prune ONLY devices that are GONE from adb (offline / moved IP) and NOT blocked.
    # A device that is adb-reachable but read-unreadable is almost always a transient
    # a11y drop or an app hiccup — NEVER unbind a real account for that, it just needs
    # a11y recovery. (A duplicate from a real IP move is resolved by canon, not here.)
    gone = [s for s in failed_bound if s not in reachable and s not in blocked]
    # SAFETY GUARD on the GONE set: post-reboot (before `adb connect`) every device
    # is "not found" = all gone. Skip pruning when the gone set exceeds a fraction
    # of the WHOLE prior roster (a systemic outage, not per-account staleness).
    _frac = float(os.environ.get("DISCOVER_PRUNE_MAX_FAIL_FRAC", "0.5"))
    pruned = []
    if prev_bound and len(gone) / prev_bound > _frac:
        print(f"[discover] {len(gone)}/{prev_bound} of the roster gone from adb -> "
              f"systemic outage suspected; SKIPPING prune (bindings preserved). "
              f"Reconnect adb, then re-run to prune genuinely-stale devices.")
    else:
        for s in gone:
            old = merged.pop(s, None)
            if old:
                pruned.append((s, old))
                if not dry_run:
                    try:
                        fleet_events.emit("unbind", device=s, account=old, reason="gone_on_bind")
                    except Exception:
                        pass
    # Flag devices whose a11y TREE is DOWN (reachable but unreadable because the
    # Mobilerun provider isn't serving) so the Control tab can mark + filter them for
    # recovery. A device that is reachable + a11y-UP but still unreadable is genuinely
    # account-less (no IG / banned / logged-out) -> left empty, NOT flagged a11y-down.
    reachable_unreadable = [s for s in unreadable if s in reachable]
    a11y_down, no_account = [], []
    if reachable_unreadable:
        states = await asyncio.gather(*[_a11y_state(s) for s in reachable_unreadable])
        a11y_down = [s for s, st in zip(reachable_unreadable, states) if st == "down"]
        if not dry_run:
            for s in a11y_down:
                try:
                    fleet_events.emit("a11y_down", device=s,
                                      account=(merged.get(s) or prev_devices.get(s)))
                except Exception:
                    pass
        if a11y_down:
            _samp = ", ".join(a11y_down[:8]) + ("…" if len(a11y_down) > 8 else "")
            print(f"[discover] a11y tree DOWN on {len(a11y_down)} reachable device(s) "
                  f"(flagged on Control tab; need a11y recovery): {_samp}")

        # AUTO-CLEAR (IP-rotation), STRONG signal only: a bound device that is
        # adb-reachable + a11y-UP + unreadable + not-blocked AND has NO Instagram
        # installed = its old IP was reassigned to a blank phone after a rotation.
        # Clear that stale binding. A device WITH IG (login challenge / logged out /
        # transient nav / adb glitch) is too ambiguous -> preserved. Guarded against
        # a systemic event the same way as prune.
        na_candidates = [s for s, st in zip(reachable_unreadable, states)
                         if st == "up" and s in prev_devices and s not in blocked]
        no_account = []
        if na_candidates:
            ig = await asyncio.gather(*[_ig_installed(s) for s in na_candidates])
            no_account = [s for s, has in zip(na_candidates, ig) if not has]
        if prev_bound and len(no_account) / prev_bound > _frac:
            print(f"[discover] {len(no_account)}/{prev_bound} bound devices IG-missing -> "
                  f"systemic; SKIPPING auto-clear (bindings kept)")
            no_account = []
        for s in no_account:
            old = merged.pop(s, None)
            if old and not dry_run:
                try:
                    fleet_events.emit("unbind", device=s, account=old, reason="no_account")
                except Exception:
                    pass
        if no_account:
            _samp = ", ".join(no_account[:8]) + ("…" if len(no_account) > 8 else "")
            print(f"[discover] {len(no_account)} bound device(s) reachable, a11y-up, IG NOT "
                  f"installed -> binding CLEARED (IP-rotation / blank phone): {_samp}")

    preserved_blocked = [s for s in failed_bound if s in blocked]
    if preserved_blocked:
        _samp = ", ".join(preserved_blocked[:8]) + ("…" if len(preserved_blocked) > 8 else "")
        print(f"[discover] {len(preserved_blocked)} blocked (login-challenge) device(s) "
              f"preserved untouched (valid account, just challenged): {_samp}")

    # Announce each (re)binding on the events stream FIRST, so the freshness signal
    # (discover ordinal) the dedup uses below already counts the serials we just
    # read live — a moved account's NEW device must out-rank its stale twin.
    for s, u in results:
        if u:
            try:
                fleet_events.emit("discover", device=s, account=u)
            except Exception:
                pass
    ai.bust_events_cache()

    # ONE-ACCOUNT-≤-ONE-SERIAL INVARIANT: auto-canon any account that ended up on
    # >1 serial (junk reads stripped too). The freshly-read serial wins via the
    # discover ordinal; stale/offline twins are dropped + audited. (Reuse the adb
    # reachability snapshot taken for the prune above.)
    deduped, dropped = ai.enforce_invariant(merged, reachable=reachable)

    if dry_run:
        print(f"\n[DRY-RUN] would write {len(deduped)} bindings "
              f"({len(added)} new, {len(changed)} changed, {len(dropped)} dup dropped, "
              f"{len(pruned)} gone-pruned, {len(no_account)} no-account cleared, "
              f"{len(a11y_down)} a11y-down flagged); roster NOT modified")
        for acc, ds, keep, reason in sorted(dropped):
            print(f"  WOULD DROP {ds} (dup of {acc}) -> canonical {keep} [{reason}]")
        for s, acct in sorted(pruned):
            print(f"  WOULD PRUNE {s} (was {acct}; gone from adb — moved IP / offline)")
        for s in sorted(no_account):
            print(f"  WOULD CLEAR {s} (a11y up, no account — IG gone/banned/logout)")
        for s in sorted(a11y_down):
            print(f"  WOULD FLAG {s} (a11y tree down — needs recovery; binding kept)")
        return 0

    payload: dict = {
        "_comment": "Account<->device binding for the fleet launcher. serial -> IG "
                    "username, discovered live via scripts/discover_fleet.py "
                    "(read from each device's profile action_bar_title). MVP local "
                    "store; gitignored. IPs rotate on router reboot — re-run after. "
                    "Enforced one-account-<=-one-serial via account_identity.",
        "devices": deduped,
    }
    for k, v in (existing or {}).items():
        if k.startswith("_") and k not in ("_comment", "_unreadable", "_duplicate_accounts"):
            payload[k] = v
    if unreadable:
        payload["_unreadable"] = unreadable
    # atomic + locked write; strips junk + asserts injective before replacing
    try:
        ai.write_roster_payload(payload)
    except OSError as e:
        print(f"[discover] roster lock busy ({e}) -> roster NOT updated this run")
        return 1
    for acc, ds, keep, reason in dropped:
        try:
            fleet_events.emit("canon", account=acc, device=keep, dropped=ds, reason=reason)
        except Exception:
            pass

    # Mirror the new roster into automation.accounts (Phase-2 durable store). MERGE
    # runs only: a --replace builds a partial roster from just this run's serials, so
    # auto-syncing would over-clear other accounts' bindings — run `account_store.py
    # --sync` explicitly after a deliberate full --replace. Best-effort: a DB hiccup
    # never fails the local bind.
    if not replace:
        try:
            import account_store
            res = account_store.sync_roster(deduped)
            print(f"  automation.accounts synced (upserted={res.get('upserted')}, cleared={res.get('cleared')})")
        except Exception as e:
            print(f"  automation.accounts sync skipped: {e}")

    # PROXY FOLLOWS THE ACCOUNT: when a device's account is NEW or CHANGED, set the
    # proxy bound to THAT account in the DB (automation -> router) — the device is
    # configured for its CURRENT worker, not the previous one. This is the per-device
    # "configure for work" step of the device-abstraction model. Runs by default for
    # the changed/added bindings only (precise, minimal outward churn);
    # DISCOVER_APPLY_PROXY=1 broadens it to reconcile ALL touched devices.
    # Best-effort + never fails the bind (router assign spends nothing).
    apply_serials = {s for s, _o, _n in changed} | set(added)
    if os.environ.get("DISCOVER_APPLY_PROXY", "0").strip().lower() in ("1", "true", "yes", "on"):
        apply_serials |= {s for s, u in results if u}
    if apply_serials:
        try:
            import account_proxy_store
            changes = account_proxy_store.apply_account_proxies(serials=apply_serials)
            if changes:
                print(f"  proxy set from DB on {len(changes)} device(s) (account new/changed): "
                      + ", ".join(f"{s}->{hp}" for s, _a, hp in changes[:6]))
        except Exception as e:
            print(f"  account-proxy apply skipped: {e}")

    print(f"\nroster now {len(deduped)} bindings ({len(added)} new, {len(changed)} changed, "
          f"{len(dropped)} dup dropped, {len(pruned)} stale pruned) -> {BINDING}")
    for s, old, new in changed:
        print(f"  CHANGED {s}: {old} -> {new}")
    for acc, ds, keep, reason in sorted(dropped):
        print(f"  DROPPED {ds} (dup of {acc}) -> canonical {keep} [{reason}]")
    for s, acct in sorted(pruned):
        print(f"  PRUNED {s} (was {acct}; unreadable on bind — likely moved IP)")
    if unreadable:
        print(f"UNREADABLE ({len(unreadable)}): {unreadable}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.exit(asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())))
    sys.exit(asyncio.run(main()))
