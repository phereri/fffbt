#!/usr/bin/env python3
"""Account-as-entity identity seam + dedup policy for the posting fleet.

The phone is an interchangeable RUNNER; the IG ACCOUNT is the worker/identity.
This module is the ONE place that answers "which account is on which device" and
"who is the canonical device for an account" — so the rest of the fleet stops
keying on the device serial and starts keying on the account.

Layered on purpose so Phase 2 (cut over to the ``automation`` schema) only has to
swap the bodies of the Group-A resolvers — every call site imports the seam:

  Group A  RESOLVERS  (data-source; today the JSON roster, tomorrow automation.*)
  Group B  POLICY     (pure; junk-filter, canonical-device precedence, invariant)
  Group C  WRITE+GUARD (atomic roster write + per-account run lock)  -- added in
                        the enforcement step; this file currently ships A + B and
                        a read-only ``__main__`` audit.

Run the audit (read-only — touches nothing):
    python scripts/account_identity.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    import msvcrt

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))

from src.runner import fleet_events  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
ACCOUNTS_ORACLE = ROOT / "data" / "known_accounts.json"
ROSTER_LOCK = ROOT / "data" / "device_accounts.lock"
LOCK_DIR = ROOT / "data" / "locks"

# Rollout switch: default OBSERVE (detect + log + surface, block nothing). Flip to
# hard-block by exporting FLEET_DEDUP_ENFORCE=1 once the dashboard shows no false
# positives. Read per-process so a child inherits the spawner's choice.
def _enforce_default() -> bool:
    return os.environ.get("FLEET_DEDUP_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on")

# Longest plausible gap with a run still "alive": one post timeout (~30m) + the
# max inter-post cadence (~45m) + slack. A start-sentinel older than this with no
# terminal is treated as a dead run (fail-OPEN) so a crash never bricks an account.
STALE_RUN_WINDOW = 1800 + 2700 + 300  # 4800s

# fleet_events types that mean "a run for this account just began / is alive"
_RUN_START = {"loop_start", "claim", "sleep", "rate_limit", "published", "stage_start"}
# ...and the ones that mean it ended. NB: 'result' is per-POST terminal (a loop
# emits one per reel), so it must NOT count as a run-end or the soft signal goes
# blind for the whole inter-post cooldown. Only true run terminals belong here.
_RUN_END = {"device_done", "fleet_child_exit"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_ts(s) -> float:
    """ISO ts (now_iso(): '...Z' or with offset) -> epoch seconds. 0.0 on failure."""
    if not s:
        return 0.0
    try:
        t = str(s).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Group A — RESOLVERS  (Phase-2 repoint target; signatures are frozen)
# ---------------------------------------------------------------------------
def roster() -> dict:
    """{serial -> account} from the binding file (absolute path). The one reader."""
    return _read_json(BINDING).get("devices") or {}


def account_for(serial: str) -> str | None:
    """The account currently bound to ``serial`` (or None). Absolute-path read —
    replaces post_scripted._account_for's cwd-relative `Path("data/...")`."""
    return roster().get(serial)


def serials_for(account: str) -> list[str]:
    """Every serial currently bound to ``account`` (reverse index)."""
    return [s for s, a in roster().items() if a == account]


_events_cache: dict = {"mtime": None, "events": None}


def _events() -> list[dict]:
    """All fleet events in chronological (file) order, cached on the jsonl mtime."""
    p = Path(fleet_events._DEFAULT_PATH)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    if _events_cache["mtime"] != mtime:
        _events_cache["events"] = fleet_events.read_events()
        _events_cache["mtime"] = mtime
    return _events_cache["events"] or []


def bust_events_cache() -> None:
    """Force the next event read to reload — call right after emitting events whose
    freshness must be reflected immediately (e.g. discover_fleet before canon)."""
    _events_cache["mtime"] = None


def discover_seq() -> dict:
    """{serial -> monotonic ordinal of its LAST 'discover' event}. The file index
    survives 1s-resolution ts ties: same-second discoveries still differ by index."""
    out: dict = {}
    for i, e in enumerate(_events()):
        if e.get("type") == "discover":
            dev = e.get("device")
            if dev:
                out[dev] = i
    return out


def discover_ts() -> dict:
    """{serial -> epoch ts of its freshest 'discover' event} (audit / readability)."""
    out: dict = {}
    for e in _events():
        if e.get("type") == "discover":
            dev = e.get("device")
            if dev:
                out[dev] = max(out.get(dev, 0.0), _parse_ts(e.get("ts")))
    return out


def last_post_ts(account: str) -> tuple:
    """Most recent (device, epoch_ts) this account actually PUBLISHED from. Requires
    real publish proof — a 'published' event, or a 'result' that actually went live
    (published/success). A bare 'claim' or a FAILED result does NOT count, so a
    blocked/a11y-down old serial can't pin canon and keep a moved account stuck."""
    dev, ts = "", 0.0
    for e in _events():
        if e.get("account") != account:
            continue
        typ = e.get("type")
        proven = (typ == "published") or (typ == "result" and (e.get("published") or e.get("success")))
        if not proven:
            continue
        t = _parse_ts(e.get("ts"))
        if t >= ts and e.get("device"):
            dev, ts = e.get("device"), t
    return dev, ts


def account_running(account: str, exclude_serial: str | None = None) -> str | None:
    """Soft signal: a serial (!= exclude_serial) on which ``account`` appears to be
    actively running — a RUN_START newer than any RUN_END for it, within the stale
    window. Fail-OPEN: a start older than the window with no terminal is ignored."""
    start_dev, start_ts, end_ts = "", 0.0, 0.0
    for e in _events():
        if e.get("account") != account:
            continue
        t = _parse_ts(e.get("ts"))
        typ = e.get("type")
        if typ in _RUN_START and e.get("device") and e.get("device") != exclude_serial:
            if t >= start_ts:
                start_dev, start_ts = e.get("device"), t
        elif typ in _RUN_END:
            end_ts = max(end_ts, t)
    if not start_dev or start_ts <= end_ts:
        return None
    if _now() - start_ts > STALE_RUN_WINDOW:  # fail-open: stale start, assume dead
        return None
    return start_dev


# ---------------------------------------------------------------------------
# Group B — POLICY  (pure; never repointed at a new data source)
# ---------------------------------------------------------------------------
_oracle_cache: set | None = None


def load_oracle() -> set:
    """Set of known-real usernames (fffbt.accounts.uid ∪ historical posted_by)."""
    global _oracle_cache
    if _oracle_cache is None:
        _oracle_cache = set(_read_json(ACCOUNTS_ORACLE).get("usernames") or [])
    return _oracle_cache


def is_real_username(u: str | None, *, seen: set | None = None) -> bool:
    """Oracle-first junk filter. True iff ``u`` is a plausible real handle:

      * in the known-accounts oracle, OR
      * already an established roster value (previously-seen / passed ``seen``),

    so the 64/106 names absent from accounts.uid are NOT false-junked. A junk
    a11y read ('action_bar_title_view', 'title_view', …) is in neither set. Shape
    is only a last-resort backstop, never the sole gate."""
    if not u or not str(u).strip():
        return False
    u = str(u).strip()
    if u in load_oracle():
        return True
    if seen and u in seen:
        return True
    # NB: do NOT accept "is currently a roster value" — junk a11y reads get
    # written to the roster too (that is the bug we are filtering out). Fall
    # through to the shape backstop, which rejects the resource-id junk class.
    low = u.lower()
    if " " in u or low.endswith("_view") or low.startswith("action_bar") or "title_view" in low:
        return False
    # shape backstop for genuine handles not yet in the oracle (brand-new binds)
    return all(c.isalnum() or c in "._" for c in u)


def _canon_with_reason(account, candidates, *, reachable=None,
                       dseq=None, dts=None) -> tuple:
    """(winner_serial | None, reason). Strict precedence — the single shared
    implementation consumed by canonical_serial AND enforce_invariant."""
    cands = list(dict.fromkeys(candidates or []))  # dedupe, keep order
    if not is_real_username(account):
        return None, "junk_account"
    if not cands:
        return None, "no_candidates"
    if len(cands) == 1:
        return cands[0], "sole"

    dseq = discover_seq() if dseq is None else dseq
    dts = discover_ts() if dts is None else dts

    # tier 1 — actively-posting device wins (it discovers LESS, so discover-ts is
    # biased to drop the working device). BUT only when no OTHER candidate was
    # discovered strictly AFTER this account's last publish — otherwise a freshly
    # read serial (an account that just MOVED here) would lose to the stale old
    # one that merely published earlier. "Freshly read serial wins" must hold.
    lp_dev, lp_ts = last_post_ts(account)
    if (lp_dev in cands and lp_ts and (_now() - lp_ts) <= STALE_RUN_WINDOW
            and not any(c != lp_dev and dts.get(c, 0.0) > lp_ts for c in cands)):
        return lp_dev, "active_posting"

    def present(s):
        return 1 if (s in dseq or s in dts) else 0

    def reach(s):
        return 1 if (reachable is not None and s in reachable) else 0

    # ordering by tiers 3(seq) > 4(ts) > 5(present) > 6(reachable) > 7(lexical)
    ordered = sorted(cands, key=lambda s: (-dseq.get(s, -1), -dts.get(s, -1.0),
                                           -present(s), -reach(s), s))
    top = ordered[0]

    # tier 2 — IP-rotation guard: if the freshest-discover twin is unreachable but
    # another is reachable, prefer reachable and shout (do NOT keep the stale IP)
    if reachable is not None and top not in reachable and any(c in reachable for c in cands):
        reachable_first = [c for c in ordered if c in reachable]
        return reachable_first[0], "ip_rotation_suspected"

    # reason = the highest distinguishing tier for the winner
    others = [c for c in cands if c != top]
    if all(dseq.get(top, -1) > dseq.get(o, -1) for o in others):
        reason = "freshest_discover"
    elif present(top) and all(not present(o) for o in others):
        reason = "only_discovered"
    elif reach(top) and all(not reach(o) for o in others):
        reason = "reachable"
    else:
        reason = "lexicographic"
    return top, reason


def canonical_serial(account: str, candidates=None, *, reachable=None) -> str | None:
    """The one serial that should own ``account`` (None if account is junk / unbound)."""
    cands = serials_for(account) if candidates is None else candidates
    return _canon_with_reason(account, cands, reachable=reachable)[0]


def enforce_invariant(devices: dict, *, reachable=None) -> tuple:
    """Collapse a {serial->account} map to one-account-≤-one-serial.

    Junk values are stripped FIRST (so a both-junk pair never reaches the dedup),
    then each account with >1 serial keeps its canonical device and drops the rest.

    Returns (deduped_map, dropped) where dropped = [(account, dropped_serial,
    kept_serial, reason), …]. Idempotent; injective-on-username."""
    dseq, dts = discover_seq(), discover_ts()
    # 1) strip junk reads up front
    clean = {s: a for s, a in devices.items() if is_real_username(a)}
    # 2) group serials by account
    by_acc: dict = {}
    for s, a in clean.items():
        by_acc.setdefault(a, []).append(s)
    deduped: dict = {}
    dropped: list = []
    for acc, serials in by_acc.items():
        if len(serials) == 1:
            deduped[serials[0]] = acc
            continue
        keep, reason = _canon_with_reason(acc, serials, reachable=reachable,
                                          dseq=dseq, dts=dts)
        if keep is None:  # shouldn't happen post junk-filter, but be safe
            continue
        deduped[keep] = acc
        for s in serials:
            if s != keep:
                dropped.append((acc, s, keep, reason))
    return deduped, dropped


# ---------------------------------------------------------------------------
# Group C — WRITE + GUARD  (atomicity + per-account run lock)
# ---------------------------------------------------------------------------
class DuplicateAccount(Exception):
    """Raised (only in ENFORCE mode) when a launch would run an account that is
    already running, on a non-canonical device, or whose lock is held."""

    def __init__(self, *, account, serial=None, canonical=None, reason=""):
        self.account, self.serial, self.canonical, self.reason = account, serial, canonical, reason
        super().__init__(f"account {account!r} dup-blocked on {serial} "
                         f"(canonical={canonical}, reason={reason})")


def _lock_path(account: str) -> Path:
    """Per-account lock file with an INJECTIVE name (sha1) — never the lossy
    char->_ collapse, so two distinct handles can't collide on one lock."""
    h = hashlib.sha1(account.encode("utf-8")).hexdigest()[:16]
    return LOCK_DIR / f"acct-{h}.lock"


@contextmanager
def _flock(path: Path, *, blocking: bool):
    """Exclusive advisory lock on ``path`` (a side-car .lock file, never the data
    file). Yields the open handle. OS releases it on close / process death."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    locked = False
    try:
        if os.name == "nt":
            f.seek(0)
            if blocking:
                for _ in range(50):           # ~5s: LK_NBLCK + short backoff
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        import time
                        time.sleep(0.1)
                if not locked:
                    raise OSError("could not acquire lock (timeout)")
            else:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
        else:  # posix fallback
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            locked = True
        yield f
    finally:
        if locked:
            try:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def _assert_injective(devices: dict) -> None:
    seen: dict = {}
    for s, a in devices.items():
        if a in seen:
            raise ValueError(f"roster not injective: account {a!r} on {seen[a]} AND {s}")
        seen[a] = s


def atomic_update_roster(mutate) -> dict:
    """Read-modify-write the roster under ROSTER_LOCK. ``mutate(devices)`` returns
    the new {serial->account} map. Junk values are stripped and injectivity is
    asserted before an atomic temp-file + os.replace. Returns the written map.
    Closes the last-writer-wins race between discover_fleet and _ensure_bound."""
    with _flock(ROSTER_LOCK, blocking=True):
        data = _read_json(BINDING)
        if "devices" not in data:
            data = {"devices": (data.get("devices") or {})}
        newdev = mutate(dict(data.get("devices") or {}))
        newdev = {s: a for s, a in newdev.items() if is_real_username(a)}
        _assert_injective(newdev)
        data["devices"] = newdev
        _atomic_replace(data)
        return newdev


def _atomic_replace(payload: dict) -> None:
    """Write ``payload`` to BINDING via temp-file + os.replace (crash-safe; a
    concurrent reader sees either the whole old or whole new file, never partial)."""
    fd, tmp = tempfile.mkstemp(dir=str(BINDING.parent), prefix=".roster.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BINDING)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_roster_payload(payload: dict) -> dict:
    """Atomically write a FULL roster payload ({_comment, devices, …}) under the
    roster lock, after stripping junk values and asserting one-account-≤-one-serial.
    Used by discover_fleet, which builds the whole payload itself."""
    with _flock(ROSTER_LOCK, blocking=True):
        devices = {s: a for s, a in (payload.get("devices") or {}).items() if is_real_username(a)}
        _assert_injective(devices)
        out = dict(payload)
        out["devices"] = devices
        _atomic_replace(out)
        return devices


@contextmanager
def account_lock(account: str, *, enforce=None):
    """Hold an exclusive per-account lock for the caller's whole lifetime. MUST be
    taken by the long-lived loop owner (post_loop/fleet_scripted), spanning the
    inter-post sleeps — not the per-post child. Yields True if the lock is held,
    False if it was contended (OBSERVE mode proceeds; ENFORCE raises)."""
    enforce = _enforce_default() if enforce is None else enforce
    if not is_real_username(account):
        # a junk/empty LOOP_ACCOUNT is a misconfigured run, never a normal post —
        # raise the same exception callers already handle, not a bare ValueError
        # that would crash the loop before it starts.
        raise DuplicateAccount(account=account, reason="junk_account")
    path = _lock_path(account)
    try:
        with _flock(path, blocking=False) as _f:
            yield True
            return
    except OSError:
        pass  # contended by a live holder
    fleet_events.emit("dup_lock", account=account,
                      mode="enforce" if enforce else "observe")
    if enforce:
        raise DuplicateAccount(account=account, reason="lock_held")
    yield False  # OBSERVE: record contention, proceed without the lock


def _launch_conflicts(serial: str, account: str):
    """(reason, conflicting_value) list for launching ``account`` on ``serial``,
    or [] if clean. Pure detection — no side effects."""
    out = []
    canon = canonical_serial(account)
    if canon is not None and canon != serial:
        out.append(("not_canonical", canon))
    bound = account_for(serial)
    if bound not in (None, account):              # absent/None is non-fatal
        out.append(("serial_disagrees", bound))
    running = account_running(account, exclude_serial=serial)
    if running:
        out.append(("already_running", running))
    return out


def assert_can_launch(serial: str, account: str, *, enforce=None) -> None:
    """Fast pre-check (no lock). OBSERVE: emit ``dup_observed`` + return. ENFORCE:
    raise DuplicateAccount on the first conflict."""
    enforce = _enforce_default() if enforce is None else enforce
    if not is_real_username(account):
        return
    conflicts = _launch_conflicts(serial, account)
    if not conflicts:
        return
    reason, other = conflicts[0]
    fleet_events.emit("dup_blocked" if enforce else "dup_observed",
                      account=account, device=serial,
                      canonical=canonical_serial(account), conflict=other,
                      reason=reason, mode="enforce" if enforce else "observe")
    if enforce:
        raise DuplicateAccount(account=account, serial=serial,
                               canonical=canonical_serial(account), reason=reason)


def validate_canon_under_lock(serial: str, account: str, *, enforce=None) -> None:
    """Re-validate canon AFTER account_lock is held (the roster may have changed
    between the pre-check and the acquire). ENFORCE raises on a flip."""
    enforce = _enforce_default() if enforce is None else enforce
    canon = canonical_serial(account)
    if canon is not None and canon != serial:
        fleet_events.emit("dup_blocked" if enforce else "dup_observed",
                          account=account, device=serial, canonical=canon,
                          reason="canon_flipped", mode="enforce" if enforce else "observe")
        if enforce:
            raise DuplicateAccount(account=account, serial=serial,
                                   canonical=canon, reason="canon_flipped")


def apply_dedup(*, reachable=None) -> tuple:
    """Collapse the live roster to one-account-≤-one-serial, atomically. Returns
    (deduped_map, dropped). Reversible: a dropped serial re-binds on next discover."""
    captured = {"dropped": []}

    def _mut(devices):
        deduped, dropped = enforce_invariant(devices, reachable=reachable)
        captured["dropped"] = dropped
        for acc, ds, keep, reason in dropped:
            fleet_events.emit("canon", account=acc, device=keep, dropped=ds, reason=reason)
        return deduped

    written = atomic_update_roster(_mut)
    return written, captured["dropped"]


# ---------------------------------------------------------------------------
# read-only audit  (python scripts/account_identity.py)
# ---------------------------------------------------------------------------
def _adb_online() -> set | None:
    """Set of adb-connected serials (state 'device'), or None if adb is unavailable."""
    adb = os.environ.get("ADB_PATH") or os.environ.get("ADB_BIN") or "adb"
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    online = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            online.add(parts[0])
    return online or None


def audit() -> int:
    devices = roster()
    reachable = _adb_online()
    print(f"roster: {len(devices)} bindings   adb-online: "
          f"{len(reachable) if reachable is not None else 'unknown'}   "
          f"oracle: {len(load_oracle())} known usernames\n")

    # junk values
    junk = {s: a for s, a in devices.items() if not is_real_username(a)}
    if junk:
        print(f"JUNK profile reads ({len(junk)}) — will be stripped, devices need re-bind:")
        for s, a in sorted(junk.items()):
            print(f"  {s:24} -> {a!r}")
        print()

    deduped, dropped = enforce_invariant(devices, reachable=reachable)
    # group drops by account
    by_acc: dict = {}
    for acc, ds, keep, reason in dropped:
        by_acc.setdefault((acc, keep, reason), []).append(ds)

    print(f"DUPLICATE accounts: {len(by_acc)}   (serials that would be dropped: {len(dropped)})\n")
    lp_window = STALE_RUN_WINDOW
    for (acc, keep, reason), drops in sorted(by_acc.items()):
        lp_dev, lp_ts = last_post_ts(acc)
        flag = ""
        # a dropped serial that recently posted = likely-wrong canon -> review
        if any(d == lp_dev for d in drops) and lp_ts and (_now() - lp_ts) <= lp_window:
            flag = "  <<< REVIEW: a DROPPED serial posted recently"
        elif reason == "ip_rotation_suspected":
            flag = "  <<< ip-rotation suspected"
        keep_state = ("online" if reachable and keep in reachable
                      else "OFFLINE" if reachable is not None else "?")
        print(f"  {acc}")
        print(f"      keep  {keep:24} [{keep_state}]  reason={reason}{flag}")
        for d in drops:
            dstate = ("online" if reachable and d in reachable
                      else "OFFLINE" if reachable is not None else "?")
            print(f"      drop  {d:24} [{dstate}]")
    print(f"\nafter dedup: {len(deduped)} bindings (was {len(devices)})")
    return 0


def apply_cli() -> int:
    """`--apply`: back up the roster, then atomically collapse duplicates."""
    reachable = _adb_online()
    before = roster()
    # timestamped backup (re-bind reference for any dropped serial)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = BINDING.parent / f"device_accounts.backup_{stamp}.json"
    backup.write_text(json.dumps({"devices": before}, ensure_ascii=False, indent=2), encoding="utf-8")
    written, dropped = apply_dedup(reachable=reachable)
    try:
        import account_store
        res = account_store.sync_roster(written)
        print(f"automation.accounts synced (upserted={res.get('upserted')}, cleared={res.get('cleared')})")
    except Exception as e:
        print(f"automation.accounts sync skipped: {e}")
    print(f"backup: {backup.name}")
    print(f"roster: {len(before)} -> {len(written)} bindings  ({len(dropped)} dup serials dropped)")
    for acc, ds, keep, reason in sorted(dropped):
        print(f"  DROPPED {ds:24} (dup of {acc}) -> canonical {keep} [{reason}]")
    junk = sorted(s for s, a in before.items() if not is_real_username(a))
    if junk:
        print(f"stripped {len(junk)} junk-read device(s) (need re-bind): {', '.join(junk)}")
    return 0


if __name__ == "__main__":
    if "--apply" in sys.argv[1:]:
        raise SystemExit(apply_cli())
    raise SystemExit(audit())
