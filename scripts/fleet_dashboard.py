#!/usr/bin/env python3
"""Local web dashboard for the Trial-Reel posting fleet.

Reads the artifacts the fleet already writes — no extra services, no external
deps (stdlib ``http.server`` only) — and renders a live view of:

  * which devices/accounts are working right now and their CURRENT stage
    (claim → prepare → publish → verify), with a live elapsed timer;
  * which videos were posted — this session and per account — with links;
  * the human-readable per-account log tail;
  * per-stage + per-video timing statistics (avg / median / min / max and each
    stage's share of total time) so you can see what to optimise;
  * session totals: posted, average time per post, throughput, error rate;
  * (optional) the Supabase backlog counts (new / posting / verify / posted).

Data sources (all produced by the existing scripts):
  data/device_accounts.json   roster (serial -> IG account)
  data/fleet_pids.json        spawned post_loop pids
  data/fleet_events.jsonl     structured event stream (src/runner/fleet_events)
  post_loop_<account>.log     per-account human log
  post_fleet.log              supervisor log
  Supabase Management API      backlog counts (best-effort, cached)

Run:
  python scripts/fleet_dashboard.py            # http://127.0.0.1:8765
  FLEET_DASH_PORT=9000 python scripts/fleet_dashboard.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.runner import fleet_events  # noqa: E402
from src.runner import s3_sync  # noqa: E402
from scripts import proxy_manager, proxy_vn  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
PIDS_FILE = ROOT / "data" / "fleet_pids.json"
FLEET_LOG = ROOT / "post_fleet.log"

HOST = os.environ.get("FLEET_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("FLEET_DASH_PORT", "8765"))
LOG_TAIL_LINES = int(os.environ.get("FLEET_DASH_LOG_LINES", "40"))

# S3 -> fffbt.videos sync (insert-only). A background daemon started in main()
# pulls new objects from the Ferma bucket into the DB every S3_SYNC_INTERVAL.
S3_SYNC = os.environ.get("S3_SYNC", "1").strip().lower() not in ("0", "false", "no", "")
S3_SYNC_INTERVAL = int(os.environ.get("S3_SYNC_INTERVAL", "300"))   # 5 minutes
_s3_sync_lock = threading.Lock()
_s3_sync_state: dict = {
    "last_run": None, "last_ok": None, "last_error": None,
    "last_inserted": 0, "inserted_total": 0, "runs": 0, "running": False,
}

POSTED_VERDICTS = {"SUCCESS", "PUBLISHED_UNCONFIRMED"}
FAIL_VERDICTS = {"FAILED", "ERROR"}
STAGES = ("prepare", "publish", "verify")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _safe_account(account: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in account)


def _parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _tail(path: Path, n: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-n:]


def _stats(values: list[float]) -> dict:
    vals = [v for v in values if isinstance(v, (int, float)) and v > 0]
    if not vals:
        return {"n": 0, "avg": 0, "median": 0, "min": 0, "max": 0}
    return {
        "n": len(vals),
        "avg": round(statistics.fmean(vals), 1),
        "median": round(statistics.median(vals), 1),
        "min": round(min(vals), 1),
        "max": round(max(vals), 1),
    }


# ---------------------------------------------------------------------------
# process liveness (which spawned pids are still running)
# ---------------------------------------------------------------------------
def _running_pids() -> set[int] | None:
    """Set of live python pids on Windows; None on posix (use os.kill).

    Filtered to ``python.exe`` so a stale pid in ``fleet_pids.json`` that the OS
    later reused for some unrelated process is not mistaken for a live loop.
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    pids: set[int] = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[1].strip().isdigit():
            pids.add(int(row[1].strip()))
    pids.discard(os.getpid())  # never count the dashboard itself (pid reuse)
    return pids


def _is_alive(pid: int | None, win_pids: set[int] | None) -> bool:
    if not pid:
        return False
    if win_pids is not None:
        return pid in win_pids
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# optional Supabase backlog counts (cached)
# ---------------------------------------------------------------------------
_db_cache: dict = {"ts": 0.0, "data": None}


def _load_env() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _db_counts() -> dict | None:
    """Backlog counts by status from Supabase; cached 60s, best-effort."""
    now = time.time()
    if _db_cache["data"] is not None and now - _db_cache["ts"] < 60:
        return _db_cache["data"]
    ref = os.environ.get("SUPABASE_PROJECT_REF", "")
    pat = os.environ.get("SUPABASE_PAT", "")
    if not ref or not pat:
        return None
    sql = (
        "SELECT status, count(*) AS n FROM fffbt.videos "
        "WHERE platform = 'Instagram' GROUP BY status"
    )
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                 "User-Agent": "fffbt-dashboard/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            rows = json.loads(resp.read())
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        counts["total"] = sum(counts.values())
        _db_cache.update(ts=now, data=counts)
        return counts
    except Exception:
        _db_cache.update(ts=now, data=None)  # don't hammer a failing API
        return None


# ---------------------------------------------------------------------------
# state assembly
# ---------------------------------------------------------------------------
def _current_status(evs: list[dict], alive: bool) -> dict:
    """Map an account's most-recent events to a human status + machine state."""
    if not evs:
        return {"state": "idle", "label": "no activity yet", "since": None}
    last = evs[-1]
    t = last.get("type")
    since = last.get("ts")

    if t == "stage_start":
        stage = last.get("stage", "?")
        label = "⏳ waiting in download queue…" if stage == "queue" else f"{stage}…"
        return {"state": "working", "stage": stage,
                "label": label, "since": since, "live_since": since}
    if t == "stage_done":
        return {"state": "working", "stage": last.get("stage"),
                "label": f"{last.get('stage')} done", "since": since}
    if t == "claim":
        return {"state": "working", "stage": "claim",
                "label": f"claimed {last.get('name') or ''}".strip(), "since": since}
    if t == "published":
        return {"state": "working", "stage": "verify",
                "label": "published, verifying…", "since": since}
    if t == "result":
        return {"state": "done", "label": f"finished: {last.get('verdict')}", "since": since}
    if t == "sleep":
        return {"state": "sleeping", "label": "cooldown · next post in",
                "since": since, "until": last.get("until")}
    if t == "rate_limit":
        return {"state": "rate_limited",
                "label": f"rate cap {last.get('count')}/{last.get('cap')} — paused",
                "since": since, "until": last.get("until")}
    if t == "recover":
        if last.get("state") == "start":
            return {"state": "recovering", "label": "recovering device (reboot)…", "since": since}
        return {"state": "working", "label": "recovered", "since": since}
    if t == "escalate":
        return {"state": "stopped", "label": f"escalated: {last.get('reason')}", "since": since}
    if t in ("loop_start",):
        return {"state": "starting", "label": "loop started", "since": since}
    if t == "fleet_child_exit":
        return {"state": "stopped", "label": f"process exited (rc={last.get('rc')})", "since": since}
    if t == "device_done":
        rc = last.get("rc")
        if rc == 0:
            lbl = f"done · {last.get('posted', 0)} posted"
        else:
            lbl = {1: "stopped — repeated failures", 3: "stopped — no videos",
                   4: "stopped — blocked", 5: "stopped — a11y down",
                   6: "stopped — trial reels not enabled", 7: "stopped — proxy down"
                   }.get(rc, f"stopped (rc={rc})")
        return {"state": "done" if rc == 0 else "stopped", "label": lbl, "since": since}
    return {"state": "unknown", "label": t or "?", "since": since}


def _account_block(account: str, device: str, all_events: list[dict],
                   session_start: float | None, win_pids: set[int] | None,
                   spawned_pids: dict[str, int],
                   active_task_serials: dict[str, int] | None = None) -> dict:
    evs = [e for e in all_events if e.get("account") == account]
    # session-scoped events for stats (still keep full history for status)
    sevs = [e for e in evs
            if session_start is None or (_parse_ts(e.get("ts")) or 0) >= session_start]

    # loop pid (latest loop_start), falling back to the supervisor's pid file
    # (covers a fleet started before it emitted any events) + liveness.
    pid = None
    for e in reversed(evs):
        if e.get("type") == "loop_start" and e.get("pid"):
            pid = int(e["pid"])
            break
    if pid is None:
        pid = spawned_pids.get(device)
    alive = _is_alive(pid, win_pids)

    results = [e for e in sevs if e.get("type") == "result"]
    posted = [r for r in results if r.get("verdict") in POSTED_VERDICTS]
    failed = [r for r in results if r.get("verdict") in FAIL_VERDICTS]

    def _stage_vals(stage: str) -> list[float]:
        return [float(r.get("timing", {}).get(stage, 0) or 0) for r in posted]

    timings = {s: _stats(_stage_vals(s)) for s in STAGES}
    timings["total"] = _stats([float(r.get("timing", {}).get("total", 0) or 0) for r in posted])

    recent = []
    for r in reversed(posted[-10:]):
        recent.append({
            "name": r.get("name"), "ts": r.get("ts"), "url": r.get("post_url"),
            "verdict": r.get("verdict"), "verify_route": r.get("verify_route"),
            "total": (r.get("timing") or {}).get("total"),
        })

    status = _current_status(evs, alive)
    # A run launched from the Control panel has no loop_start/fleet_pids entry, so
    # the pid-based liveness above misses it. If this device is covered by a
    # RUNNING control task and its latest activity is a working event, it's alive.
    if (not alive and status.get("state") in ("working", "sleeping", "rate_limited")
            and (active_task_serials or {}).get(device)):
        alive = True
        pid = pid or active_task_serials[device]
    if not alive and status["state"] not in ("stopped", "done"):
        status = {"state": "offline", "label": "process not running", "since": status.get("since")}

    log_path = ROOT / f"post_loop_{_safe_account(account)}.log"
    return {
        "account": account,
        "device": device,
        "pid": pid,
        "alive": alive,
        "in_task": bool((active_task_serials or {}).get(device)),  # has a running task
        "status": status,
        "counts": {
            "attempts": len(results),
            "posted": len(posted),
            "confirmed": sum(1 for r in posted if r.get("verdict") == "SUCCESS"),
            "unconfirmed": sum(1 for r in posted if r.get("verdict") == "PUBLISHED_UNCONFIRMED"),
            "failed": len(failed),
        },
        "timings": timings,
        "recent": recent,
        "last_post_ts": posted[-1]["ts"] if posted else None,
        "log": _tail(log_path, LOG_TAIL_LINES),
    }


def build_state() -> dict:
    roster = _read_json(BINDING).get("devices", {}) or {}
    all_events = fleet_events.read_events()

    # session = since the most recent fleet_start (fall back to first event)
    session_start = None
    session_start_ts = None
    for e in reversed(all_events):
        if e.get("type") == "fleet_start":
            session_start = _parse_ts(e.get("ts"))
            session_start_ts = e.get("ts")
            break
    fleet_stopped = False
    if session_start_ts is not None:
        # a fleet_stop AFTER the latest fleet_start means the supervisor ended
        for e in reversed(all_events):
            if e.get("type") == "fleet_stop" and (e.get("ts") or "") > session_start_ts:
                fleet_stopped = True
            if e.get("type") == "fleet_start":
                break

    win_pids = _running_pids()
    # fleet_pids.json maps pid -> serial; invert for a serial -> pid fallback.
    spawned_pids = {serial: int(pid) for pid, serial in _read_json(PIDS_FILE).items()
                    if str(pid).isdigit()}
    # serials covered by a RUNNING Control-panel task -> mark those accounts alive
    active_task_serials: dict[str, int] = {}
    try:
        for t in list_tasks():
            if t.get("running"):
                for s in t.get("active_devices", []):  # only devices still working
                    active_task_serials[s] = t.get("pid")
    except Exception:
        pass
    accounts = [_account_block(acct, serial, all_events, session_start, win_pids,
                               spawned_pids, active_task_serials)
                for serial, acct in roster.items()]

    # ---- session aggregate ----
    sresults = [e for e in all_events if e.get("type") == "result"
                and (session_start is None or (_parse_ts(e.get("ts")) or 0) >= session_start)]
    posted = [r for r in sresults if r.get("verdict") in POSTED_VERDICTS]
    failed = [r for r in sresults if r.get("verdict") in FAIL_VERDICTS]

    agg_stage = {s: _stats([float(r.get("timing", {}).get(s, 0) or 0) for r in posted]) for s in STAGES}
    agg_total = _stats([float(r.get("timing", {}).get("total", 0) or 0) for r in posted])
    total_avg = agg_total["avg"] or 0
    stage_share = {s: (round(agg_stage[s]["avg"] / total_avg * 100, 1) if total_avg else 0)
                   for s in STAGES}

    elapsed_hr = 0.0
    if session_start:
        elapsed_hr = max((time.time() - session_start) / 3600.0, 0.0)
    throughput = round(len(posted) / elapsed_hr, 2) if elapsed_hr > 0.05 else 0

    # session feed (latest events, human-ish)
    feed = []
    for e in all_events[-60:]:
        feed.append({k: e.get(k) for k in ("ts", "type", "account", "device", "stage",
                                           "verdict", "name", "reason", "seconds", "until")
                     if e.get(k) is not None})

    session_posts = []
    for r in reversed([r for r in posted][-40:]):
        session_posts.append({
            "ts": r.get("ts"), "account": r.get("account"), "name": r.get("name"),
            "verdict": r.get("verdict"), "url": r.get("post_url"),
            "verify_route": r.get("verify_route"),
            "total": (r.get("timing") or {}).get("total"),
        })

    return {
        "now": fleet_events.now_iso(),
        "fleet": {
            "session_start": session_start_ts,
            "stopped": fleet_stopped,
            "devices_total": len(roster),
            "devices_active": sum(1 for a in accounts if a["alive"]),
        },
        "summary": {
            "attempts": len(sresults),
            "posted": len(posted),
            "confirmed": sum(1 for r in posted if r.get("verdict") == "SUCCESS"),
            "unconfirmed": sum(1 for r in posted if r.get("verdict") == "PUBLISHED_UNCONFIRMED"),
            "failed": len(failed),
            "error_rate": round(len(failed) / len(sresults) * 100, 1) if sresults else 0,
            "avg_total": agg_total["avg"],
            "throughput_per_hr": throughput,
            "elapsed_hr": round(elapsed_hr, 2),
            "download_queue": sum(1 for a in accounts
                                  if (a.get("status") or {}).get("stage") == "queue"),
        },
        "stage_stats": {**agg_stage, "total": agg_total, "share": stage_share},
        "accounts": accounts,
        "session_posts": session_posts,
        "feed": list(reversed(feed)),
        "fleet_log": _tail(FLEET_LOG, 30),
        "backlog": _db_counts(),
        "s3_sync": _s3_sync_view(),
    }


# ---------------------------------------------------------------------------
# Device control panel — list adb devices + start/stop scripts on selections.
# Kept entirely separate from the account-stats above (own endpoints + view).
# ---------------------------------------------------------------------------
CONTROL_TASKS_FILE = ROOT / "data" / "control_tasks.json"
CONTROL_LOG_DIR = ROOT / "data" / "control_logs"
CONTROL_STOP_DIR = ROOT / "data" / "control_stop"     # graceful-stop flag files
STOP_GRACE_SECS = int(os.environ.get("STOP_GRACE_SECS", "420"))  # wait for in-flight verify
TRAJ_DIR = ROOT / "trajectories" / "scripted"
# Always spawn tasks with the PROJECT venv python (it has the deps). Falling back
# to sys.executable was a footgun: if the dashboard itself was started with the
# bare system python, it spawned dep-less runs in parallel.
_venv_py = ROOT / ".venv" / "Scripts" / ("python.exe" if os.name == "nt" else "python")
VENV_PY = str(_venv_py) if _venv_py.exists() else sys.executable

# Whitelisted, parameterised actions (no arbitrary command execution). Each runs
# one of OUR scripts on the selected serials. device_arg: "flag" => `--devices s…`,
# "positional" => `s …`. {stagger} is substituted from the request.
ACTIONS: dict[str, dict] = {
    "discover": {"label": "Discover account (bind)", "script": "scripts/discover_fleet.py",
                 "device_arg": "positional", "extra": [],
                 "env": {}, "needs_devices": True, "danger": False},
    "post":    {"label": "Post Trial Reels", "script": "scripts/fleet_scripted.py",
                "device_arg": "flag", "env": {"HUMANIZE": "1"},
                "needs_devices": True, "danger": True, "modal": True},
    "dryrun":  {"label": "Dry-run (no publish)", "script": "scripts/fleet_scripted.py",
                "device_arg": "flag", "env": {"HUMANIZE": "0"},
                "needs_devices": True, "danger": False},
    "verify":  {"label": "Check a11y", "script": "scripts/recover_a11y.py",
                "device_arg": "positional", "extra": ["--verify-only"],
                "env": {}, "needs_devices": True, "danger": False},
    "recover": {"label": "Recover a11y (reboot)", "script": "scripts/recover_a11y.py",
                "device_arg": "positional", "extra": [],
                "env": {}, "needs_devices": True, "danger": True},
}

_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def _adb_bin() -> str:
    return os.environ.get("ADB_PATH") or os.environ.get("ADB_BIN") or "adb"


def list_adb_devices() -> list[dict] | None:
    """All adb-connected serials (state) joined with the account roster. Roster
    devices that are not currently connected are included as 'disconnected'.
    Returns None if adb itself could not be run."""
    roster = _read_json(BINDING).get("devices", {}) or {}
    try:
        out = subprocess.run([_adb_bin(), "devices"], capture_output=True, text=True, timeout=12).stdout
    except Exception:
        return None
    devices, seen = [], set()
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = (p.strip() for p in line.split("\t", 1))
        seen.add(serial)
        devices.append({"serial": serial, "state": state,
                        "account": roster.get(serial), "in_roster": serial in roster})
    for serial, acct in roster.items():
        if serial not in seen:
            devices.append({"serial": serial, "state": "disconnected",
                            "account": acct, "in_roster": True})
    devices.sort(key=lambda d: (d["account"] or "~", d["serial"]))
    return devices


def _persist_tasks() -> None:  # call under _tasks_lock
    try:
        CONTROL_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {tid: {k: v for k, v in t.items() if k != "proc"} for tid, t in _tasks.items()}
        CONTROL_TASKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_tasks() -> None:
    """Re-attach tasks from a previous dashboard run (proc handle is gone, so we
    track them by pid only — status via liveness, stop via taskkill)."""
    data = _read_json(CONTROL_TASKS_FILE)
    if not isinstance(data, dict):
        return
    with _tasks_lock:
        for tid, t in data.items():
            t = dict(t)
            t["proc"] = None
            _tasks[tid] = t


def _iopt(opts: dict, key: str, default: int) -> int:
    try:
        return int(opts.get(key, default))
    except (TypeError, ValueError):
        return default


def _flag_extra(action: str, opts: dict) -> list[str]:
    """fleet_scripted args for the post / dry-run actions, from the request opts."""
    e = ["--stagger", str(max(0, _iopt(opts, "stagger", 20))),
         "--category", str(opts.get("category") or "trend"),
         "--order", ("desc" if str(opts.get("order", "")).lower() == "desc" else "asc")]
    if action == "dryrun":
        return e + ["--no-share", "--count", "1"]
    if opts.get("loop"):
        e += ["--loop"]
    else:
        e += ["--count", str(max(1, _iopt(opts, "count", 1)))]
    e += ["--delay-min", str(max(0, _iopt(opts, "delay_min", 900))),
          "--delay-max", str(max(0, _iopt(opts, "delay_max", 2700))),
          "--max-24h", str(max(0, _iopt(opts, "max_24h", 20)))]
    return e


_done_cache: dict = {"ts": 0.0, "data": None}


_TERMINAL_STOP = {"BLOCKED", "TRIAL_UNAVAILABLE", "PROXY_DOWN", "NO_ROWS"}


def _device_done_map() -> dict:
    """serial -> latest epoch at which its per-device loop ENDED. A new fleet emits an
    explicit 'device_done' event; for robustness (and older fleets that don't) a
    terminal-stop 'result' (BLOCKED / TRIAL_UNAVAILABLE / PROXY_DOWN / NO_ROWS) counts
    too, since those verdicts always break the device loop. Lets the dashboard free a
    finished/blocked device from a still-running task."""
    now = time.time()
    if _done_cache["data"] is not None and now - _done_cache["ts"] < 5:
        return _done_cache["data"]
    m: dict[str, float] = {}
    try:
        for e in fleet_events.read_events():
            dev = e.get("device")
            if not dev:
                continue
            t = e.get("type")
            ended = (t == "device_done") or (t == "result" and e.get("verdict") in _TERMINAL_STOP)
            # a later 'claim' means the device started working again -> it's NOT done
            if t == "claim":
                m.pop(dev, None)
                continue
            if ended:
                ts = _parse_ts(e.get("ts")) or 0.0
                if ts >= m.get(dev, 0.0):
                    m[dev] = ts
    except Exception:
        pass
    _done_cache.update(ts=now, data=m)
    return m


_stop_cache: dict = {"ts": 0.0, "data": None}

# rc (device_done exit code / result rc) -> short human label for WHY a device's
# posting loop ended. Mirrors fleet_scripted's verdict map; rc 0/2 are "posted"
# outcomes, the rest are terminal stops (incl. rc 8 trial-reel limit).
_STOP_LABELS = {
    0: "done", 1: "repeated failures (5x)", 2: "posted (unconfirmed)",
    3: "no videos available", 4: "blocked (login challenge)", 5: "accessibility down",
    6: "trial reels not enabled", 7: "proxy down", 8: "trial-reel limit reached",
}
_RC_VERDICT = {
    0: "SUCCESS", 2: "PUBLISHED_UNCONFIRMED", 3: "NO_ROWS", 4: "BLOCKED",
    5: "A11Y_DOWN", 6: "TRIAL_UNAVAILABLE", 7: "PROXY_DOWN", 8: "TRIAL_LIMIT",
}
# Verdicts that immediately END the loop and are its true terminal reason -- once
# seen in a run they must not be overwritten by a later success/"done" outcome.
_TERMINAL_VERDICTS = {"BLOCKED", "TRIAL_UNAVAILABLE", "PROXY_DOWN", "TRIAL_LIMIT",
                      "NO_ROWS", "A11Y_DOWN"}


def _stop_label(rc) -> str:
    return _STOP_LABELS.get(rc, f"stopped (rc={rc})" if rc is not None else "stopped")


def _device_stop_map() -> dict:
    """{by_dev, by_acc}: the latest run-ENDING outcome per device/account as
    {rc, verdict, label, since}. Primary source is the 'device_done' event (its rc
    IS the loop's exit code -- the most reliable stop reason); 'result' events are a
    fallback. A later 'claim' clears the entry (the device started a new run, so the
    old stop reason is stale)."""
    now = time.time()
    if _stop_cache["data"] is not None and now - _stop_cache["ts"] < 10:
        return _stop_cache["data"]
    by_dev: dict[str, dict] = {}
    by_acc: dict[str, dict] = {}

    def _put(store, key, ts, rc, verdict, posted=None):
        if not key:
            return
        cur = store.get(key)
        # A terminal stop verdict is the loop's REAL reason; don't let a later
        # success-ish outcome bury it. (device_done's rc collapses to 0 once the
        # device posted at least once, so a posted-then-TRIAL_LIMIT run would
        # otherwise read as "done"; the same run's terminal 'result' must win.)
        if cur is not None and cur.get("verdict") in _TERMINAL_VERDICTS and verdict not in _TERMINAL_VERDICTS:
            return
        if cur is None or ts >= cur["since"]:
            lbl = f"done · {posted} posted" if (rc == 0 and posted is not None) else _stop_label(rc)
            store[key] = {"rc": rc, "verdict": verdict, "label": lbl, "since": ts}

    try:
        for e in fleet_events.read_events():
            t = e.get("type")
            ts = _parse_ts(e.get("ts")) or 0.0
            dev, acc = e.get("device"), e.get("account")
            if t == "claim":
                by_dev.pop(dev, None)
                by_acc.pop(acc, None)
                continue
            if t == "device_done":
                # last_rc is the real terminal code; rc collapses to 0 if it posted.
                rc = e.get("last_rc", e.get("rc"))
                _put(by_dev, dev, ts, rc, _RC_VERDICT.get(rc, "FAILED"), e.get("posted"))
                _put(by_acc, acc, ts, rc, _RC_VERDICT.get(rc, "FAILED"), e.get("posted"))
            elif t == "result":
                _put(by_dev, dev, ts, e.get("rc"), e.get("verdict"))
                _put(by_acc, acc, ts, e.get("rc"), e.get("verdict"))
    except Exception:
        pass
    res = {"by_dev": by_dev, "by_acc": by_acc}
    _stop_cache.update(ts=now, data=res)
    return res


def _task_active_devices(task: dict, done: dict | None = None) -> list[str]:
    """Devices of a RUNNING task that haven't finished yet (no device_done since the
    task started). A finished device is released from the task."""
    done = _device_done_map() if done is None else done
    started = task.get("started") or 0
    return [s for s in task.get("devices", []) if done.get(s, 0.0) < started]


def start_task(action: str, devices: list[str], opts: dict | None = None) -> dict:
    opts = opts or {}
    cfg = ACTIONS.get(action)
    if not cfg:
        raise ValueError(f"unknown action {action!r}")
    serials = [s for s in (devices or []) if s]
    if cfg.get("needs_devices") and not serials:
        raise ValueError("select at least one device")
    # guard: never assign a device that's already ACTIVE in a running task (a device
    # whose loop has ended is free again, even if the task is still running for others)
    busy = {s for t in list_tasks() if t.get("running") for s in t.get("active_devices", [])}
    clash = [s for s in serials if s in busy]
    if clash:
        raise ValueError(f"already busy in a running task: {', '.join(clash)}")
    tid = uuid.uuid4().hex[:8]
    CONTROL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CONTROL_LOG_DIR / f"{tid}.log"
    args = [VENV_PY, str(ROOT / cfg["script"])]
    if cfg.get("device_arg") == "flag":
        if serials:
            args += ["--devices", *serials]
        args += _flag_extra(action, opts)
    else:  # positional
        args += [*serials, *cfg.get("extra", [])]
    CONTROL_STOP_DIR.mkdir(parents=True, exist_ok=True)
    stop_flag = CONTROL_STOP_DIR / f"{tid}.flag"
    try:
        stop_flag.unlink()                              # clear any stale flag
    except FileNotFoundError:
        pass
    env = dict(os.environ)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                "FLEET_STOP_FLAG": str(stop_flag)})     # graceful-stop signal file
    env.update(cfg.get("env", {}))
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    logf = open(log_path, "ab")
    logf.write(f"$ {' '.join(args[1:])}\n".encode("utf-8"))
    logf.flush()
    proc = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT,
                            cwd=str(ROOT), env=env, creationflags=flags)
    label = cfg["label"]
    if action == "post":
        label += " (loop)" if opts.get("loop") else f" x{max(1, _iopt(opts, 'count', 1))}"
    rec = {"id": tid, "action": action, "label": label, "devices": serials,
           "pid": proc.pid, "started": time.time(), "stop_flag": str(stop_flag),
           "cmd": " ".join(args[1:]), "log": str(log_path)}
    with _tasks_lock:
        _tasks[tid] = {**rec, "proc": proc}
        _persist_tasks()
    return rec


def _unclaim_task(task: dict) -> int:
    """Reset to 'new' any video this task CLAIMED but did NOT post (still 'posting').
    Verifying/posted reels (status 'verify'/'posted') are left untouched, so a live
    reel's link is never lost. Attribution is by this task's own 'claim' events since
    it started, so other running tasks' in-flight claims are never affected."""
    devices = set(task.get("devices", []))
    started = task.get("started") or 0
    claimed: set[str] = set()
    finished: set[str] = set()
    try:
        for e in fleet_events.read_events():
            if (_parse_ts(e.get("ts")) or 0) < started or e.get("device") not in devices:
                continue
            vid = e.get("video_id")
            if not vid:
                continue
            if e.get("type") == "claim":
                claimed.add(vid)
            elif e.get("type") in ("published", "result"):
                finished.add(vid)        # reached publish/verify -> NOT unclaimed
    except Exception:
        return 0
    cand = [v for v in claimed if v not in finished]
    if not cand:
        return 0
    idlist = ", ".join("'" + str(v).replace("'", "''") + "'" for v in cand)
    try:
        rows = _mgmt_query("UPDATE fffbt.videos SET status='new' WHERE status='posting' "
                           f"AND id IN ({idlist}) RETURNING id")
        return len(rows)
    except Exception:
        return 0


def _graceful_stop_worker(tid: str) -> None:
    """Wait for the task to finish in-flight work (incl. verify) after the stop flag is
    set, force-kill if it overruns the grace window (e.g. an old cycle that ignores the
    flag), then unclaim its un-posted videos."""
    with _tasks_lock:
        t = dict(_tasks.get(tid) or {})
    if not t:
        return
    pid = t.get("pid")
    # Only wait for a graceful exit if this task can actually see the stop flag (a
    # NEW-code cycle started with FLEET_STOP_FLAG). An old cycle ignores the flag and
    # would keep claiming during any wait, so stop it at once.
    if t.get("stop_flag"):
        deadline = time.time() + STOP_GRACE_SECS
        while time.time() < deadline and _is_alive(pid, _running_pids()):
            time.sleep(3)
    if _is_alive(pid, _running_pids()):                 # didn't exit gracefully -> force
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=12)
            else:
                os.kill(int(pid), 15)
        except Exception:
            pass
        time.sleep(2)
    n = _unclaim_task(t)
    print(f"[stop {tid}] stopped; unclaimed {n} un-posted video(s)", flush=True)
    try:
        Path(t["stop_flag"]).unlink()
    except Exception:
        pass


def stop_task(tid: str) -> bool:
    """GRACEFUL stop: signal the cycle to stop claiming new videos and finish any
    in-flight post (verify included, so a live reel's link is captured); a sleeping
    device stops at once. A background worker then force-kills on overrun and unclaims
    un-posted videos."""
    with _tasks_lock:
        t = _tasks.get(tid)
        if not t:
            return False
        t["stopping"] = time.time()
        flag = t.get("stop_flag")
        _persist_tasks()
    try:
        if flag:
            CONTROL_STOP_DIR.mkdir(parents=True, exist_ok=True)
            Path(flag).write_text("stop", encoding="utf-8")
    except Exception:
        pass
    threading.Thread(target=_graceful_stop_worker, args=(tid,), daemon=True).start()
    return True


def _clear_finished() -> None:
    win_pids = _running_pids()
    with _tasks_lock:
        for tid in [t["id"] for t in _tasks.values() if not _task_running(t, win_pids)]:
            _tasks.pop(tid, None)
        _persist_tasks()


def _task_running(t: dict, win_pids: set[int] | None) -> bool:
    proc = t.get("proc")
    if proc is not None:
        return proc.poll() is None
    return _is_alive(t.get("pid"), win_pids)


def list_tasks() -> list[dict]:
    win_pids = _running_pids()
    done = _device_done_map()
    out = []
    with _tasks_lock:
        for t in _tasks.values():
            proc = t.get("proc")
            if proc is not None and proc.poll() is not None and t.get("rc") is None:
                t["rc"] = proc.returncode
            running = _task_running(t, win_pids)
            if not running and not t.get("ended"):
                t["ended"] = time.time()      # freeze the end time once it stops
            end = t["ended"] if (not running and t.get("ended")) else time.time()
            # devices still working (the rest have ended and are freed for reuse)
            active = [s for s in t["devices"] if done.get(s, 0.0) < (t.get("started") or 0)] if running else []
            out.append({
                "id": t["id"], "action": t["action"], "label": t["label"],
                "devices": t["devices"], "active_devices": active,
                "pid": t["pid"], "started": t["started"],
                "running": running, "rc": (None if running else t.get("rc")),
                "stopping": bool(running and t.get("stopping")),
                "runtime": round(end - t["started"]),
                "cmd": t.get("cmd", ""),
                "log_tail": _tail(Path(t["log"]), 14) if t.get("log") else [],
            })
        _persist_tasks()
    out.sort(key=lambda x: x["started"], reverse=True)
    return out


# --- login-challenge / BLOCKED state, derived from the signals the scripts emit
# (fleet_events result verdict=BLOCKED + trajectory hard_stop/login_challenge).
# The MOST RECENT outcome per serial/account wins, so a later success clears it.
_challenge_cache: dict = {"ts": 0.0, "data": None}


def _challenge_map() -> dict:
    now = time.time()
    if _challenge_cache["data"] is not None and now - _challenge_cache["ts"] < 45:
        return _challenge_cache["data"]
    by_serial: dict[str, dict] = {}
    by_account: dict[str, dict] = {}

    def _upd(d: dict, key, ts: float, blocked: bool, reason: str) -> None:
        if not key:
            return
        cur = d.get(key)
        if cur is None or ts >= cur["ts"]:
            d[key] = {"ts": ts, "blocked": blocked, "reason": reason}

    try:  # fleet_events: 'result' rows set/clear block; 'discover' clears it
        for e in fleet_events.read_events():
            t = e.get("type")
            ts = _parse_ts(e.get("ts")) or 0.0
            if t == "result":
                blk = e.get("verdict") == "BLOCKED"
                reason = e.get("code") or e.get("verdict") or ""
                _upd(by_serial, e.get("device"), ts, blk, reason)
                _upd(by_account, e.get("account"), ts, blk, reason)
            elif t == "discover":
                # a fresh (re)binding to a readable account = not blocked
                _upd(by_serial, e.get("device"), ts, False, "rebound")
                _upd(by_account, e.get("account"), ts, False, "rebound")
    except Exception:
        pass

    try:  # per-run trajectories — capture publish-only runs too (e.g. the .50 probe)
        for d in sorted(TRAJ_DIR.glob("*/"), reverse=True)[:200]:
            jf = d / "trajectory.jsonl"
            if not jf.exists():
                continue
            serial = account = None
            outcome = None  # (ts, blocked, reason)
            for line in jf.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                serial = serial or ev.get("serial")
                et = ev.get("event")
                if et == "run_start":
                    account = ev.get("account")
                if et in ("login_challenge", "hard_stop"):
                    outcome = (ev.get("ts") or 0.0, True, ev.get("reason") or "login_challenge")
                elif et == "DEVIATION" and str(ev.get("step", "")).startswith("hard_stop"):
                    outcome = (ev.get("ts") or 0.0, True, "login_challenge")
                elif et == "run_result":
                    outcome = (ev.get("ts") or 0.0, ev.get("verdict") == "BLOCKED", ev.get("verdict") or "")
            if outcome:
                _upd(by_serial, serial, *outcome)
                _upd(by_account, account, *outcome)
    except Exception:
        pass

    data = {"by_serial": by_serial, "by_account": by_account}
    _challenge_cache.update(ts=now, data=data)
    return data


_cat_cache: dict = {"ts": 0.0, "data": None}


def _category_counts() -> list[dict]:
    """[{category, new}] — count of status='new' Instagram videos per category. Cached 60s."""
    now = time.time()
    if _cat_cache["data"] is not None and now - _cat_cache["ts"] < 60:
        return _cat_cache["data"]
    ref = os.environ.get("SUPABASE_PROJECT_REF", "")
    pat = os.environ.get("SUPABASE_PAT", "")
    if not ref or not pat:
        return []
    sql = ("SELECT category, count(*) AS n FROM fffbt.videos "
           "WHERE status='new' AND platform='Instagram' "
           "GROUP BY category ORDER BY category")
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                 "User-Agent": "fffbt-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            rows = json.loads(resp.read())
        data = [{"category": str(r["category"]), "new": int(r["n"])}
                for r in rows if r.get("category")]
        _cat_cache.update(ts=now, data=data)
        return data
    except Exception:
        _cat_cache.update(ts=now, data=[])
        return []


def control_state() -> dict:
    devs = list_adb_devices()
    tasks = list_tasks()
    # serial -> label of the RUNNING task that owns it (can't run two on one device)
    busy: dict[str, str] = {}
    for t in tasks:
        if t.get("running"):
            for s in t.get("active_devices", []):     # finished devices are free again
                busy.setdefault(s, t.get("label") or t.get("action"))
    if devs:
        cm = _challenge_map()
        sm = _device_stop_map()
        for d in devs:
            cands = [c for c in (cm["by_serial"].get(d["serial"]),
                                 cm["by_account"].get(d.get("account"))) if c]
            latest = max(cands, key=lambda c: c["ts"]) if cands else None
            d["blocked"] = bool(latest and latest["blocked"])
            d["block_reason"] = latest["reason"] if d["blocked"] else None
            d["block_since"] = latest["ts"] if d["blocked"] else None
            d["busy"] = busy.get(d["serial"])
            # why the last run ended (TRIAL_LIMIT / TRIAL_UNAVAILABLE / repeated
            # failures / proxy down / done / ...), for non-blocked active accounts.
            st = sm["by_dev"].get(d["serial"]) or sm["by_acc"].get(d.get("account"))
            d["stop_verdict"] = st["verdict"] if st else None
            d["stop_label"] = st["label"] if st else None
            d["stop_since"] = st["since"] if st else None
    return {
        "adb_ok": devs is not None,
        "devices": devs or [],
        "blocked_count": sum(1 for d in (devs or []) if d.get("blocked")),
        "actions": [{"id": k, "label": v["label"], "danger": v.get("danger", False)}
                    for k, v in ACTIONS.items()],
        "categories": _category_counts(),
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default request logging
        pass

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            try:
                body = json.dumps(build_state(), ensure_ascii=False).encode("utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                return self._send(500, body, "application/json; charset=utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if path == "/api/control/state":
            try:
                body = json.dumps(control_state(), ensure_ascii=False).encode("utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if path == "/api/proxy/state":
            try:
                body = json.dumps(proxy_manager.status(), ensure_ascii=False).encode("utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def _json_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        p = self._json_body()
        if path == "/api/control/run":
            try:
                rec = start_task(p.get("action", ""), p.get("devices") or [], p)
                return self._send(200, json.dumps({"ok": True, "task": rec}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path == "/api/control/stop":
            ok = stop_task(p.get("id", ""))
            return self._send(200 if ok else 404, json.dumps({"ok": ok}).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/control/clear":
            _clear_finished()
            return self._send(200, json.dumps({"ok": True}).encode("utf-8"),
                              "application/json; charset=utf-8")
        if path == "/api/proxy/renew":
            # body: {items:[{idproxy, provider}]} — renews SPEND money
            try:
                res = proxy_manager.renew_many(p.get("items") or [])
                return self._send(200, json.dumps({"ok": True, "results": res}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path == "/api/proxy/buy":
            # body: {provider, count, days} — SPENDS money
            try:
                prov = p.get("provider") or "Viettel"
                rows = proxy_vn.buy_proxies(prov, int(p.get("count", 1)), int(p.get("days", 30)))
                n = proxy_manager.record_bought(rows, prov)
                return self._send(200, json.dumps({"ok": True, "bought": len(rows), "stored": n,
                                                   "proxies": rows}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path == "/api/proxy/sync":
            try:
                n = proxy_manager.sync()
                return self._send(200, json.dumps({"ok": True, "synced": n}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        if path == "/api/proxy/rotate":
            # body: {device|ip, provider?} — buy a fresh proxy (SPENDS money) and
            # assign it to the device, replacing whatever it had.
            try:
                ip = str(p.get("device") or p.get("ip") or "").split(":", 1)[0]
                if not ip:
                    raise ValueError("no device")
                res = proxy_manager.replace_proxy_for_device(ip, p.get("provider") or "Viettel")
                return self._send(200, json.dumps({"ok": bool(res.get("ok")), "result": res}).encode("utf-8"),
                                  "application/json; charset=utf-8")
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
                                  "application/json; charset=utf-8")
        return self._send(404, b"not found", "text/plain")


PROXY_RENEW_INTERVAL = int(os.environ.get("PROXY_RENEW_INTERVAL", "3600"))   # hourly
PROXY_AUTORENEW = os.environ.get("PROXY_AUTORENEW", "1").strip().lower() not in ("0", "false", "no", "")


def _proxy_autorenew_loop() -> None:
    """Background daemon: auto-renew proxies of in-work (not Blocked) devices that
    expire in < 1 day. Renews SPEND money but only on managed, in-work proxies."""
    time.sleep(120)                                    # let the dashboard settle first
    while True:
        try:
            res = proxy_manager.renew_due(dry_run=False)
            done = [r for r in res.get("renewed", []) if r.get("ok")]
            if res.get("candidates"):
                print(f"[proxy-autorenew] {len(done)}/{len(res['candidates'])} renewed: "
                      f"{[c['account'] for c in res['candidates']]}", flush=True)
        except Exception as e:
            print(f"[proxy-autorenew] error: {e}", flush=True)
        time.sleep(PROXY_RENEW_INTERVAL)


def _s3_sync_loop() -> None:
    """Background daemon: pull new S3 objects into fffbt.videos every
    S3_SYNC_INTERVAL. Insert-only — deletions in S3 never touch the DB."""
    time.sleep(10)                                     # let the dashboard settle first
    while True:
        with _s3_sync_lock:
            _s3_sync_state["running"] = True
        try:
            res = s3_sync.sync_once(max_age_days=s3_sync.MAX_AGE_DAYS)
            now = time.time()
            with _s3_sync_lock:
                _s3_sync_state.update(
                    last_run=now, last_ok=now, last_error=None,
                    last_inserted=res.inserted,
                    inserted_total=_s3_sync_state["inserted_total"] + res.inserted,
                    runs=_s3_sync_state["runs"] + 1, running=False,
                )
            print(f"[s3-sync] +{res.inserted} new ({res.skipped} existing, "
                  f"{res.skipped_old} too old (>{s3_sync.MAX_AGE_DAYS}d), "
                  f"{res.folders} folders, {res.folders_skipped} no-meta)", flush=True)
        except Exception as e:
            with _s3_sync_lock:
                _s3_sync_state.update(last_run=time.time(), last_error=str(e), running=False)
            print(f"[s3-sync] error: {e}", flush=True)
        time.sleep(S3_SYNC_INTERVAL)


def _s3_sync_view() -> dict:
    """Sync status for the dashboard: online flag + age since last success."""
    with _s3_sync_lock:
        s = dict(_s3_sync_state)
    last_ok = s["last_ok"]
    age = (time.time() - last_ok) if last_ok else None
    # "online" = a successful pass within the last ~2 intervals (tolerates one miss).
    online = age is not None and age < S3_SYNC_INTERVAL * 2 + 60
    return {
        "enabled": S3_SYNC,
        "online": online,
        "age_sec": int(age) if age is not None else None,
        "interval_sec": S3_SYNC_INTERVAL,
        "running": s["running"],
        "last_error": s["last_error"],
        "last_inserted": s["last_inserted"],
        "inserted_total": s["inserted_total"],
        "runs": s["runs"],
    }


def main() -> int:
    _load_env()
    _load_tasks()
    if PROXY_AUTORENEW:
        threading.Thread(target=_proxy_autorenew_loop, daemon=True).start()
    if S3_SYNC:
        threading.Thread(target=_s3_sync_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"fleet dashboard on {url}  (events={fleet_events._DEFAULT_PATH})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        srv.server_close()
    return 0


# ---------------------------------------------------------------------------
# embedded UI
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Fleet Dashboard</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#30363d;
    --fg:#e6edf3; --muted:#8b949e; --accent:#3fb950; --warn:#d29922;
    --bad:#f85149; --info:#58a6ff; --purple:#bc8cff;
    color-scheme:dark;            /* dark native controls, dropdowns & scrollbars */
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg);color:var(--fg);
    -webkit-text-size-adjust:100%;text-size-adjust:100%;
    -webkit-tap-highlight-color:transparent}
  a{color:var(--info);text-decoration:none} a:hover{text-decoration:underline}
  header{display:flex;align-items:center;gap:16px;padding:14px 20px;
    border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
  header h1{font-size:16px;margin:0;font-weight:600}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
  .grow{flex:1}
  .muted{color:var(--muted)}
  .wrap{padding:18px 20px;max-width:1500px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .card .k{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
  .card .v{font-size:26px;font-weight:650;margin-top:4px}
  .card .sub{font-size:12px;color:var(--muted);margin-top:2px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
    margin:24px 0 10px;font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
  .bar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden;display:flex}
  .bar i{display:block;height:100%}
  .devgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px}
  /* compact list view */
  .devlist{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--panel)}
  .lrow{border-bottom:1px solid var(--line)}
  .lrow:last-child{border-bottom:none}
  .lrow>summary{list-style:none;cursor:pointer;display:grid;align-items:center;gap:10px;
    grid-template-columns:14px minmax(120px,1.4fr) minmax(120px,1fr) 70px 70px 64px 70px 18px;
    padding:5px 14px;user-select:none}
  .lrow>summary::-webkit-details-marker{display:none}
  .lrow>summary:hover{background:var(--panel2)}
  .lrow[open]>summary{background:var(--panel2);border-bottom:1px solid var(--line)}
  .lrow .lname{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .lrow .lstat{text-align:right;font-variant-numeric:tabular-nums}
  .lrow .lstat b{font-size:15px}
  .lrow .lstate{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
  .lrow .chev{color:var(--muted);transition:transform .15s;text-align:center}
  .lrow[open] .chev{transform:rotate(90deg)}
  .lrow .ldetail{padding:6px 14px 14px;background:var(--bg)}
  .lhead{display:grid;gap:10px;
    grid-template-columns:14px minmax(120px,1.4fr) minmax(120px,1fr) 70px 70px 64px 70px 18px;
    padding:6px 14px;font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
  .lhead .lstat{text-align:right}
  .viewtoggle{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;
    margin-left:10px;vertical-align:middle}
  .viewtoggle button{background:var(--panel);color:var(--muted);border:none;padding:4px 12px;
    font:inherit;font-size:12px;cursor:pointer}
  .viewtoggle button.on{background:var(--info);color:#0d1117;font-weight:600}
  /* device controls + pagination */
  .devctrl{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
  .devctrl input,.devctrl select{background:var(--panel);color:var(--fg);
    border:1px solid var(--line);border-radius:7px;padding:6px 9px;font:inherit;font-size:13px}
  .devctrl input[type=search]{min-width:200px}
  .devctrl label{display:inline-flex;align-items:center;gap:6px;color:var(--muted)}
  .devctrl label select{color:var(--fg)}
  .dirbtn{background:var(--panel);color:var(--fg);border:1px solid var(--line);
    border-radius:7px;padding:6px 11px;cursor:pointer;font:inherit}
  .dirbtn:hover{background:var(--panel2)}
  .pager{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:14px;
    color:var(--muted);font-size:13px}
  .pager button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
    border-radius:7px;padding:5px 12px;cursor:pointer;font:inherit}
  .pager button:disabled{opacity:.4;cursor:default}
  .pager button:not(:disabled):hover{background:var(--panel2)}
  .dev{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .dev .top{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line)}
  .dev .top .name{font-weight:650}
  .dev .body{padding:12px 14px}
  .pill{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--panel2);
    border:1px solid var(--line);white-space:nowrap}
  .stat-row{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 10px}
  .stat-row div b{font-size:16px}
  .mini{font-size:12px;color:var(--muted)}
  pre.log{background:#0a0d12;border:1px solid var(--line);border-radius:8px;padding:10px;
    max-height:200px;overflow:auto;font:11.5px/1.4 ui-monospace,Consolas,monospace;
    white-space:pre-wrap;word-break:break-word;margin:8px 0 0}
  details>summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:8px;user-select:none}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:900px){.grid2{grid-template-columns:1fr}}
  .tag{font-size:10.5px;padding:1px 6px;border-radius:5px;border:1px solid var(--line)}
  .badge{font-weight:600}
  .right{text-align:right}
  .spin{animation:sp 1.1s linear infinite;display:inline-block}
  @keyframes sp{to{transform:rotate(360deg)}}
  /* tabs */
  .tabs{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-left:8px}
  .tabs button{background:var(--panel);color:var(--muted);border:none;padding:6px 14px;
    font:inherit;font-size:13px;cursor:pointer}
  .tabs button.on{background:var(--info);color:#0d1117;font-weight:600}
  /* device control panel */
  .ctl-toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .ctl-toolbar select,.ctl-toolbar input{background:var(--panel2);color:var(--fg);
    border:1px solid var(--line);border-radius:7px;padding:7px 9px;font:inherit;font-size:13px}
  .ctl-toolbar input[type=number]{width:64px}
  .btn-go{background:var(--accent);color:#0d1117;border:none;border-radius:8px;padding:8px 16px;
    font:inherit;font-weight:650;cursor:pointer;font-size:13px}
  .btn-go:hover:not(:disabled){filter:brightness(1.1)}
  .btn-go:disabled{opacity:.45;cursor:default}
  .ctl-selbtns a{color:var(--info)}
  .ctl-devbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
  .ctl-devbar input[type=search]{background:var(--panel);color:var(--fg);border:1px solid var(--line);
    border-radius:7px;padding:6px 9px;font:inherit;font-size:13px;min-width:180px}
  .ctl-devbar label{display:inline-flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer}
  .ctl-devbar select{background:var(--panel);color:var(--fg);border:1px solid var(--line);
    border-radius:7px;padding:5px 7px;font:inherit;font-size:12px;cursor:pointer}
  .ctl-cols{display:grid;grid-template-columns:1.05fr 1fr;gap:20px}
  @media(max-width:980px){.ctl-cols{grid-template-columns:1fr}}
  .chead,.crow{display:grid;gap:8px;align-items:center;
    grid-template-columns:56px minmax(110px,1.3fr) minmax(120px,1fr) 100px;padding:8px 12px}
  .hall{display:inline-flex;align-items:center;gap:5px;cursor:pointer;color:var(--muted)}
  .hall input{width:15px;height:15px;accent-color:var(--info);cursor:pointer;margin:0}
  .crow.busy{opacity:.55}
  .crow.busy:hover{background:transparent;cursor:default}
  .bbusy{color:var(--info);font-size:10px;font-weight:600;border:1px solid var(--info);
    border-radius:5px;padding:0 5px;white-space:nowrap}
  .chead{font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);
    border-bottom:1px solid var(--line)}
  .crow{border-bottom:1px solid var(--line);cursor:pointer}
  .crow:last-child{border-bottom:none}
  .crow:hover{background:var(--panel2)}
  .crow input[type=checkbox]{width:16px;height:16px;accent-color:var(--info);cursor:pointer;margin:0}
  .crow .cacct{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .crow .cser{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
  .crow .cstate{text-align:right;font-size:11px;font-weight:600}
  .crow .cstop{display:block;font-size:10px;font-weight:400;color:var(--muted);white-space:normal;margin-top:1px;line-height:1.2}
  .crow .cstop.bad{color:var(--warn)}
  .task{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px}
  .task .thead{display:flex;align-items:center;gap:8px}
  .task .tname{font-weight:650}
  .btn-stop{background:var(--panel2);color:var(--bad);border:1px solid var(--bad);
    border-radius:7px;padding:4px 11px;cursor:pointer;font:inherit;font-size:12px}
  .btn-stop:hover{background:var(--bad);color:#0d1117}
  .task-filters{display:flex;gap:8px;margin-bottom:10px}
  .task-filters select{background:var(--panel);color:var(--fg);border:1px solid var(--line);
    border-radius:7px;padding:5px 8px;font:inherit;font-size:12.5px}
  .ctl-actions{display:inline-flex;gap:6px;flex-wrap:wrap}
  .ctl-actbtn{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
    border-radius:7px;padding:6px 11px;font:inherit;font-size:12.5px;cursor:pointer}
  .ctl-actbtn:hover:not(:disabled){background:var(--line)}
  .ctl-actbtn:disabled{opacity:.4;cursor:default}
  .ctl-actbtn.primary{background:var(--accent);color:#0d1117;font-weight:650;border-color:var(--accent)}
  .ctl-actbtn.primary:hover:not(:disabled){filter:brightness(1.08);background:var(--accent)}
  .ctl-actbtn.danger{border-color:var(--bad);color:var(--bad)}
  .ctl-actbtn.danger:hover:not(:disabled){background:var(--bad);color:#0d1117}
  .modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;
    justify-content:center;z-index:50}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;
    width:390px;max-width:92vw;box-shadow:0 12px 40px rgba(0,0,0,.5)}
  .modal h3{font-size:15px}
  .pm-row{display:flex;align-items:center;gap:8px;margin:9px 0;flex-wrap:wrap}
  .pm-row label{display:inline-flex;align-items:center;gap:6px;cursor:pointer;color:var(--fg)}
  .modal input[type=number]{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
    border-radius:6px;padding:5px 7px;font:inherit;width:60px}
  .modal select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
    border-radius:6px;padding:5px 7px;font:inherit;font-size:13px}
  .modal input[type=radio]{accent-color:var(--info);cursor:pointer}
  .pm-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}
  .bblk{color:var(--bad);font-size:10px;font-weight:700;border:1px solid var(--bad);
    border-radius:5px;padding:0 5px;white-space:nowrap}
  .crow.blk{background:rgba(248,81,73,.09)}
  .crow.blk:hover{background:rgba(248,81,73,.16)}
  #ctlBlkCount{color:var(--bad);font-weight:600}
  /* a table that can't shrink scrolls horizontally inside its own box */
  .tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

  /* ===================== MOBILE / NARROW SCREENS ===================== */
  @media (max-width:700px){
    .wrap{padding:12px 12px;overflow-x:clip}   /* clip (not hidden) keeps sticky header working */
    h2{margin:18px 0 8px}

    /* header → compact; tabs become a full-width segmented control on row 2 */
    header{flex-wrap:wrap;gap:6px 10px;padding:8px 12px}
    header h1{font-size:15px}
    header .grow{display:none}
    #fleetState{order:2;font-size:11px}
    #refresh{order:3;margin-left:auto}
    header .tabs{order:5;flex:1 1 100%;margin-left:0}
    header .tabs button{flex:1;padding:9px 6px;font-size:13px}
    #s3sync{order:6;font-size:11px}
    #clock{order:7;font-size:11px}

    /* 16px form text stops iOS auto-zoom on focus + enlarges the tap area */
    input,select,textarea{font-size:16px !important}
    .crow input[type=checkbox],.ctl-devbar input[type=checkbox],.hall input{width:20px;height:20px}
    /* comfortable ~40px tap targets for the small buttons */
    .dirbtn,.ctl-actbtn,.btn-go,.btn-stop,.pager button,.viewtoggle button{
      min-height:40px;display:inline-flex;align-items:center;justify-content:center}

    /* summary cards: keep a comfy 2-up */
    .cards{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}
    .card .v{font-size:23px}

    /* data tables: tighter, and scroll the wide one instead of overflowing */
    table{font-size:12px}
    th,td{padding:6px 6px}
    #posts{overflow-x:auto;-webkit-overflow-scrolling:touch}
    #posts table{min-width:480px}

    /* device CARDS: one per row (minmax 420 used to push the page wide) */
    .devgrid{grid-template-columns:1fr;gap:10px}

    /* device LIST rows: reflow the 8-col grid into a stacked, labelled card */
    .lhead{display:none}
    .lrow>summary{display:flex;flex-wrap:wrap;align-items:baseline;
      gap:3px 12px;padding:10px 12px}
    .lrow>summary .dot{align-self:center}
    .lrow>summary .lname{flex:1 1 auto;font-size:14px}
    .lrow>summary .chev{order:0;margin-left:auto;align-self:center}
    .lrow>summary .lstate{order:1;flex:1 1 100%;text-align:left;font-size:12.5px}
    .lrow>summary .lstat{order:2;text-align:left;font-size:11px;color:var(--muted)}
    .lrow>summary .lstat b{font-size:13px}
    .lrow>summary .lstat:nth-child(4)::before{content:"posted "}
    .lrow>summary .lstat:nth-child(5)::before{content:"failed "}
    .lrow>summary .lstat:nth-child(6)::before{content:"avg "}
    .lrow>summary .lstat:nth-child(7)::before{content:"last "}
    .lrow .ldetail{padding:8px 12px 12px}

    /* stats filter bar: full-width search, the rest wraps beneath it */
    .devctrl input[type=search]{flex:1 1 100%;min-width:0}
    .devctrl .grow{display:none}

    /* control panel: device rows become 2-line (account over serial, state right) */
    .ctl-cols{gap:14px}
    .chead{display:flex;align-items:center;padding:8px 12px}
    .chead>span{display:none}                  /* keep just the "All" checkbox */
    .crow{grid-template-columns:26px 1fr auto;
      grid-template-areas:"ck acct st" "ck ser st";gap:1px 8px;padding:10px 12px}
    .crow>input[type=checkbox]{grid-area:ck;align-self:center}
    .crow .cacct{grid-area:acct}
    .crow .cser{grid-area:ser}
    .crow .cstate{grid-area:st;align-self:center}
    .ctl-toolbar{padding:10px 12px;gap:8px 10px}
    .ctl-devbar input[type=search]{flex:1 1 100%;min-width:0}
    .ctl-devbar .grow{display:none}
    .task-filters{flex-wrap:wrap}
    .task-filters select{flex:1 1 45%}

    /* modal: near full-width and scrollable when taller than the screen */
    .modal{width:100%;max-width:94vw;max-height:90vh;overflow:auto;padding:18px 16px}

    /* select-device links + "clear finished": real tap targets (were tiny inline anchors) */
    .ctl-selbtns a,#ctlClear{display:inline-block;padding:7px 8px;min-height:34px;line-height:1.7}
    /* tap feedback on touch (where :hover never fires) */
    .crow:active,.lrow>summary:active{background:var(--panel2)}
    .dirbtn:active,.ctl-actbtn:active,.btn-go:active,.btn-stop:active{filter:brightness(1.12)}

    /* live feed + supervisor log: keep the aligned columns, scroll instead of wrapping to noise */
    #feed,#fleetlog{white-space:pre;word-break:normal;font-size:10.5px;line-height:1.35;
      overflow-x:auto;-webkit-overflow-scrolling:touch}

    /* BLOCKED device cell: stack the badge over the state text instead of staggering it */
    .crow .cstate{display:flex;flex-direction:column;align-items:flex-end;gap:2px;white-space:normal}
    /* one-line account / serial in the reflowed rows */
    .crow .cser{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .lrow>summary .lname{min-width:0;overflow:hidden;text-overflow:ellipsis}

    /* device card header: let the "device · pid" pill wrap rather than clip */
    .dev .top{flex-wrap:wrap}
    .dev .top .name{min-width:0;overflow:hidden;text-overflow:ellipsis}

    /* detail tables (recent posts): wrap long video filenames rather than clipping them */
    .ldetail table td,.dev .body table td{word-break:break-word}

    /* number fields fit 16px text + native stepper (delay/cap/stagger can be 3–4 digits) */
    .modal input[type=number],.ctl-toolbar input[type=number]{width:82px}
    .modal input[type=radio]{width:18px;height:18px}

    /* toolbars (control actions + proxy buy/renew) pack into tidy full-width rows */
    .ctl-toolbar .grow{display:none}
    .ctl-toolbar>label,.ctl-toolbar .btn-go,.ctl-toolbar .dirbtn{flex:1 1 auto}

    /* task cards: let a long label push the stop button onto its own line */
    .task .thead{flex-wrap:wrap;gap:4px 8px}
    .task .t-log{font-size:10.5px}
    /* keep card heights even when the sub-text is long */
    .card .sub{overflow-wrap:anywhere}

    /* proxy table (9 cols): more room so the two action buttons sit on one line (scrolls in .tscroll) */
    .pxtable{min-width:760px}
    .pxtable td:last-child{white-space:nowrap}
  }

  @media (max-width:400px){
    .wrap{padding:10px 10px}
    header h1{font-size:14px}
    #clock{display:none}            /* least-critical info on the tiniest screens */
    .card .v{font-size:21px}
  }
</style>
</head>
<body>
<header>
  <h1>📱 Fleet Dashboard</h1>
  <span class="tabs" id="tabs">
    <button data-tab="control" class="on">🎛 Control</button>
    <button data-tab="stats">📊 Posting Stats</button>
    <button data-tab="proxy">🌐 Proxies</button>
  </span>
  <span id="fleetState" class="pill">…</span>
  <span class="grow"></span>
  <span class="muted" id="s3sync" title="S3 → fffbt.videos sync"></span>
  <span class="muted" id="clock"></span>
  <span class="muted" id="refresh">⟳</span>
</header>
<div class="wrap" id="controlView">
  <div class="ctl-toolbar">
    <span class="mini">on&nbsp;selected&nbsp;→</span>
    <div id="ctlActions" class="ctl-actions"></div>
    <label class="mini">stagger&nbsp;<input type="number" id="ctlStagger" value="20" min="0" max="600"/>s</label>
    <span id="ctlSelCount" class="mini"></span>
    <span class="grow"></span>
    <span id="ctlAdb" class="pill">adb…</span>
    <button id="ctlReload" class="dirbtn">⟳ devices</button>
  </div>
  <div class="ctl-cols">
    <div>
      <h2>Devices <span class="mini" id="ctlDevCount"></span></h2>
      <div class="ctl-devbar">
        <input type="search" id="ctlSearch" placeholder="🔍 filter account / serial…" autocomplete="off"/>
        <label class="mini"><input type="checkbox" id="ctlRosterOnly" checked/> roster only</label>
        <label class="mini">show
          <select id="ctlBlockFilter">
            <option value="all">all</option>
            <option value="unblocked">unblocked</option>
            <option value="blocked">blocked</option>
          </select>
        </label>
        <label class="mini">sort
          <select id="ctlSort">
            <option value="account">account</option>
            <option value="serial">serial</option>
            <option value="state">adb state</option>
            <option value="stop">stop reason</option>
          </select>
        </label>
        <label class="mini">reason
          <select id="ctlStopFilter">
            <option value="all">all</option>
            <option value="none">none (no run)</option>
            <option value="SUCCESS">done</option>
            <option value="PUBLISHED_UNCONFIRMED">posted (unconfirmed)</option>
            <option value="TRIAL_LIMIT">trial-reel limit</option>
            <option value="TRIAL_UNAVAILABLE">trial reels not enabled</option>
            <option value="BLOCKED">blocked</option>
            <option value="A11Y_DOWN">accessibility down</option>
            <option value="PROXY_DOWN">proxy down</option>
            <option value="NO_ROWS">no videos</option>
            <option value="FAILED">repeated failures</option>
          </select>
        </label>
        <span class="grow"></span>
        <span class="mini ctl-selbtns">select
          <a href="#" id="ctlSelAll">all shown</a> ·
          <a href="#" id="ctlSelNone">none</a> ·
          <a href="#" id="ctlSelOnline">online</a> ·
          <a href="#" id="ctlSelRoster">roster</a> ·
          <a href="#" id="ctlSelBlocked">blocked</a>
        </span>
      </div>
      <div class="devlist" id="ctlDevs"></div>
    </div>
    <div>
      <h2>Tasks <a href="#" class="mini" id="ctlClear">— clear finished</a></h2>
      <div class="task-filters">
        <select id="tfStatus" title="filter by status">
          <option value="">all statuses</option>
          <option value="active">active</option>
          <option value="success">success</option>
          <option value="failed">failed</option>
        </select>
        <select id="tfCmd" title="filter by command"><option value="">all commands</option></select>
      </div>
      <div id="ctlTasks"></div>
    </div>
  </div>
  <div id="postModal" class="modal-bg" style="display:none">
    <div class="modal">
      <h3 style="margin:0 0 4px">Post Trial Reels</h3>
      <div class="mini" id="pmCount" style="margin-bottom:12px"></div>
      <div class="pm-row">category <select id="pmCat"></select>
        <span class="mini" id="pmCatN"></span></div>
      <div class="pm-row">order
        <label><input type="radio" name="pmorder" value="asc" checked> oldest first</label>
        <label><input type="radio" name="pmorder" value="desc"> newest first</label></div>
      <hr style="border:none;border-top:1px solid var(--line);margin:12px 0">
      <div class="pm-row"><label><input type="radio" name="pmmode" value="count" checked> post a fixed number</label>
        <input type="number" id="pmN" value="1" min="1" max="500"> <span class="mini">reels / device</span></div>
      <div class="pm-row"><label><input type="radio" name="pmmode" value="loop"> loop continuously</label></div>
      <hr style="border:none;border-top:1px solid var(--line);margin:12px 0">
      <div class="pm-row">delay between posts
        <input type="number" id="pmDmin" value="15" min="0"> – <input type="number" id="pmDmax" value="45" min="0"> <span class="mini">min</span></div>
      <div class="pm-row">24h cap / account
        <input type="number" id="pmCap" value="20" min="0"> <span class="mini">(0 = off)</span></div>
      <div class="pm-actions">
        <button id="pmCancel" class="dirbtn">Cancel</button>
        <button id="pmStart" class="btn-go">▶ Start posting</button>
      </div>
    </div>
  </div>
</div>

<div class="wrap" id="statsView" style="display:none">
  <div class="cards" id="cards"></div>

  <h2>Stage timing — what to optimise</h2>
  <div id="stages"></div>

  <h2>Devices <span class="mini" id="devcount"></span>
    <span class="viewtoggle" id="viewtoggle">
      <button data-view="cards">▦ Cards</button>
      <button data-view="list">☰ List</button>
    </span>
  </h2>
  <div class="devctrl" id="devctrl">
    <input type="search" id="fSearch" placeholder="🔍 account / device…" autocomplete="off"/>
    <select id="fState" title="filter by status">
      <option value="">all statuses</option>
      <option value="active">▶ active (has task)</option>
      <option value="working">working</option>
      <option value="sleeping">sleeping</option>
      <option value="rate_limited">rate-limited</option>
      <option value="recovering">recovering</option>
      <option value="starting">starting</option>
      <option value="done">done</option>
      <option value="stopped">stopped</option>
      <option value="offline">offline</option>
      <option value="idle">idle</option>
    </select>
    <select id="fAlive" title="filter by process">
      <option value="">any process</option>
      <option value="alive">alive only</option>
      <option value="dead">not running</option>
    </select>
    <span class="grow"></span>
    <label class="mini">sort
      <select id="fSort">
        <option value="account">account</option>
        <option value="state">status</option>
        <option value="posted">posted</option>
        <option value="failed">failed</option>
        <option value="avg">avg time</option>
        <option value="last">last post</option>
      </select>
    </label>
    <button id="fDir" class="dirbtn" title="toggle direction">▲</button>
    <label class="mini" id="fSizeWrap">per page
      <select id="fSize">
        <option value="10">10</option>
        <option value="20">20</option>
        <option value="50">50</option>
        <option value="0">all</option>
      </select>
    </label>
  </div>
  <div id="devs"></div>
  <div class="pager" id="pager"></div>

  <div class="grid2">
    <div>
      <h2>Posted this session</h2>
      <div id="posts"></div>
    </div>
    <div>
      <h2>Live event feed</h2>
      <pre class="log" id="feed" style="max-height:360px"></pre>
      <h2>Supervisor log</h2>
      <pre class="log" id="fleetlog"></pre>
    </div>
  </div>
</div>

<div class="wrap" id="proxyView" style="display:none">
  <div id="pxCards" class="cards"></div>
  <div class="ctl-toolbar">
    <button id="pxRenewSel" class="dirbtn">↻ Renew selected</button>
    <span id="pxSelCount" class="mini"></span>
    <span class="grow"></span>
    <label class="mini"><input type="checkbox" id="pxManagedOnly"/> managed only</label>
    <label class="mini"><input type="checkbox" id="pxWorkOnly"/> in-work only</label>
    <button id="pxSync" class="dirbtn" title="refresh expiry from proxy.vn">⟳ sync</button>
    <button id="pxReload" class="dirbtn">⟳ refresh</button>
  </div>
  <div class="ctl-toolbar" style="border-top:none;padding-top:0">
    <span class="mini">buy&nbsp;→</span>
    <select id="pxBuyProv" class="mini"><option>Viettel</option><option>VNPT</option><option>FPT</option></select>
    <label class="mini">count&nbsp;<input type="number" id="pxBuyCount" value="5" min="1" max="100" style="width:60px"/></label>
    <label class="mini">days&nbsp;<select id="pxBuyDays"><option>30</option><option>60</option><option>90</option></select></label>
    <button id="pxBuy" class="dirbtn">＋ Buy proxies</button>
    <span id="pxMsg" class="mini"></span>
  </div>
  <div class="tscroll"><table class="pxtable"><thead><tr>
    <th><input type="checkbox" id="pxAll"/></th>
    <th>device</th><th>account</th><th>proxy</th><th>provider</th>
    <th class="right">expires</th><th>health</th><th>status</th><th></th>
  </tr></thead><tbody id="pxRows"></tbody></table></div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const fmtDur = s => { if(s==null) return '—'; s=Math.round(s); if(s<60) return s+'s';
  const m=Math.floor(s/60), r=s%60; return r? `${m}m${r}s` : `${m}m`; };
const ago = ts => { if(!ts) return ''; const d=(Date.now()-Date.parse(ts))/1000;
  if(d<0) return 'in '+fmtDur(-d); if(d<60) return Math.round(d)+'s ago';
  if(d<3600) return Math.round(d/60)+'m ago'; return Math.round(d/3600)+'h ago'; };
const until = ts => { if(!ts) return ''; const d=(Date.parse(ts)-Date.now())/1000;
  return d>0? 'in '+fmtDur(d) : 'now'; };

const STATE_COLOR = { working:'var(--info)', sleeping:'var(--purple)', done:'var(--accent)',
  rate_limited:'var(--warn)', recovering:'var(--warn)', starting:'var(--info)',
  stopped:'var(--bad)', offline:'var(--bad)', idle:'var(--muted)', unknown:'var(--muted)' };

function card(k,v,sub,color){
  return `<div class="card"><div class="k">${esc(k)}</div>
    <div class="v" ${color?`style="color:${color}"`:''}>${v}</div>
    <div class="sub">${sub||''}</div></div>`;
}

function renderCards(d){
  const s=d.summary, b=d.backlog;
  const cards=[
    card('Posted (session)', s.posted, `${s.confirmed} confirmed · ${s.unconfirmed} unconfirmed`, 'var(--accent)'),
    card('Error rate', s.error_rate+'%', `${s.failed} failed / ${s.attempts} attempts`,
         s.error_rate>25?'var(--bad)':(s.error_rate>0?'var(--warn)':'var(--accent)')),
    card('Avg / post', fmtDur(s.avg_total), 'full prepare→verify'),
    card('Throughput', s.throughput_per_hr+'/hr', `${s.elapsed_hr}h elapsed`),
    card('Active devices', d.fleet.devices_active+'/'+d.fleet.devices_total, 'processes alive'),
    card('Download queue', s.download_queue||0, 'videos waiting to download',
         s.download_queue>0?'var(--warn)':'var(--muted)'),
  ];
  if(b) cards.push(card('Backlog (new)', b.new||0,
     `${b.posted||0} posted · ${b.verify||0} verify · ${b.total||0} total`, 'var(--info)'));
  $('#cards').innerHTML=cards.join('');
}

function renderStages(d){
  const ss=d.stage_stats, order=['prepare','publish','verify'];
  const colors={prepare:'var(--info)',publish:'var(--purple)',verify:'var(--warn)'};
  const total=ss.total.avg||0;
  let bar='<div class="bar" style="margin:6px 0 14px">';
  order.forEach(s=>{ const w=total?(ss[s].avg/total*100):0;
    bar+=`<i style="width:${w}%;background:${colors[s]}" title="${s} ${fmtDur(ss[s].avg)}"></i>`; });
  bar+='</div>';
  let rows=order.map(s=>`<tr>
     <td><span class="tag" style="border-color:${colors[s]};color:${colors[s]}">${s}</span></td>
     <td class="right">${fmtDur(ss[s].avg)}</td>
     <td class="right">${fmtDur(ss[s].median)}</td>
     <td class="right">${fmtDur(ss[s].min)}</td>
     <td class="right">${fmtDur(ss[s].max)}</td>
     <td class="right"><b>${ss.share[s]}%</b></td>
   </tr>`).join('');
  rows+=`<tr><td><b>total</b></td><td class="right"><b>${fmtDur(ss.total.avg)}</b></td>
     <td class="right">${fmtDur(ss.total.median)}</td><td class="right">${fmtDur(ss.total.min)}</td>
     <td class="right">${fmtDur(ss.total.max)}</td><td class="right">100%</td></tr>`;
  $('#stages').innerHTML = bar + `<table><thead><tr>
     <th>stage</th><th class="right">avg</th><th class="right">median</th>
     <th class="right">min</th><th class="right">max</th><th class="right">share of total</th>
   </tr></thead><tbody>${rows}</tbody></table>
   <div class="mini" style="margin-top:6px">Based on ${ss.total.n} completed posts this session. The widest bar / highest share is the best optimisation target.</div>`;
}

let VIEW = localStorage.getItem('fleetView') || 'cards';
const CTRL = {                // filter / sort / pagination state (persisted)
  search:'', state:'', alive:'',
  sort: localStorage.getItem('fleetSort') || 'account',
  dir:  localStorage.getItem('fleetDir')  || 'asc',
  size: parseInt(localStorage.getItem('fleetSize') ?? '10', 10),
  page: 1,
};

// how many cards fit per row — mirrors the CSS grid
// (repeat(auto-fill, minmax(420px,1fr)) with a 14px gap)
function cardsPerRow(){
  const w=($('#devs').clientWidth)||1200, min=420, gap=14;
  return Math.max(1, Math.floor((w+gap)/(min+gap)));
}

function processAccounts(accounts){
  const q=CTRL.search.trim().toLowerCase();
  let rows=accounts.filter(a=>{
    if(q && !((a.account||'').toLowerCase().includes(q) || (a.device||'').toLowerCase().includes(q))) return false;
    if(CTRL.state==='active'){ if(!a.in_task) return false; }
    else if(CTRL.state && (a.status||{}).state!==CTRL.state) return false;
    if(CTRL.alive==='alive' && !a.alive) return false;
    if(CTRL.alive==='dead' && a.alive) return false;
    return true;
  });
  const val=a=>{
    switch(CTRL.sort){
      case 'posted': return a.counts.posted;
      case 'failed': return a.counts.failed;
      case 'avg':    return a.timings.total.avg||0;
      case 'last':   return a.last_post_ts? Date.parse(a.last_post_ts):0;
      case 'state':  return (a.status||{}).state||'';
      default:       return (a.account||'').toLowerCase();
    }
  };
  rows.sort((x,y)=>{ const a=val(x),b=val(y); const c=a<b?-1:a>b?1:0; return CTRL.dir==='asc'?c:-c; });
  return rows;
}

function stageLineHTML(st){
  const liveStage = st.state==='working' && st.live_since;
  let s = esc(st.label||'');
  if(liveStage) s += ` <span class="spin">⏱</span><span data-live="${st.live_since}"></span>`;
  else if(st.until) s += ` <span class="spin">⏳</span><span data-until="${st.until}">${until(st.until).replace(/^in /,'')}</span>`;
  else if(st.since) s += ` <span class="muted">· ${ago(st.since)}</span>`;
  return s;
}

// status signature: rebuild the status line only when one of these changes
// (it contains a live timer child we don't want to recreate every 3s tick).
function stageSig(st){ return [st.state,st.label,st.since,st.until,st.live_since].join('|'); }

function recentRowsHTML(a){
  return (a.recent||[]).slice().reverse().map(r=>{
    const link = r.url? `<a href="${esc(r.url)}" target="_blank">link</a>` : '<span class="muted">no link</span>';
    const vc = r.verdict==='SUCCESS'?'var(--accent)':'var(--warn)';
    return `<tr><td>${esc(r.name||'—')}</td>
      <td><span class="tag" style="color:${vc};border-color:${vc}">${esc(r.verdict||'')}</span></td>
      <td class="right">${fmtDur(r.total)}</td><td class="right">${ago(r.ts)}</td>
      <td class="right">${link}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="muted">no posts yet</td></tr>';
}

// the collapsible detail skeleton — built ONCE per node so its <details> are
// never recreated (open state survives every refresh natively).
function detailSkeleton(){
  return `<div class="mini d-stageavg"></div>
    <details><summary class="d-recsum"></summary>
      <table style="margin-top:6px"><tbody class="d-recbody"></tbody></table>
    </details>
    <details><summary class="d-logsum"></summary>
      <pre class="log d-logpre"></pre>
    </details>`;
}

// Build a persistent DOM node for one account, plus an update() that writes ONLY
// the leaf values that actually changed. Nothing is destroyed on refresh, so an
// expanded <details> stays open and the scroll position is kept.
function buildDevNode(view, a){
  const root=document.createElement(view==='list'?'details':'div');
  if(view==='list'){
    root.className='lrow';
    root.innerHTML =
      `<summary>
        <span class="dot"></span>
        <span class="lname">@${esc(a.account)} <span class="mini">${esc(a.device)}</span></span>
        <span class="lstate"></span>
        <span class="lstat"><b class="s-posted" style="color:var(--accent)"></b></span>
        <span class="lstat"><b class="s-failed"></b></span>
        <span class="lstat"><b class="s-avg"></b></span>
        <span class="lstat mini"><span class="s-last"></span></span>
        <span class="chev">▸</span>
      </summary>
      <div class="ldetail">${detailSkeleton()}</div>`;
  } else {
    root.className='dev';
    root.innerHTML =
      `<div class="top">
        <span class="dot"></span>
        <span class="name">@${esc(a.account)}</span>
        <span class="grow"></span>
        <span class="pill mini s-dev"></span>
      </div>
      <div class="body">
        <div class="c-state" style="font-weight:600;margin-bottom:8px"></div>
        <div class="stat-row">
          <div><b class="s-posted" style="color:var(--accent)"></b> <span class="mini">posted</span></div>
          <div><b class="s-confirmed"></b> <span class="mini">confirmed</span></div>
          <div><b class="s-unconf" style="color:var(--warn)"></b> <span class="mini">unconf.</span></div>
          <div><b class="s-failed"></b> <span class="mini">failed</span></div>
          <div><b class="s-avg"></b> <span class="mini">avg</span></div>
        </div>
        ${detailSkeleton()}
      </div>`;
  }
  const q=s=>root.querySelector(s);
  const refs={
    dot:q('.dot'), state:q(view==='list'?'.lstate':'.c-state'),
    posted:q('.s-posted'), failed:q('.s-failed'), avg:q('.s-avg'),
    last:q('.s-last'), confirmed:q('.s-confirmed'), unconf:q('.s-unconf'), devpill:q('.s-dev'),
    stageAvg:q('.d-stageavg'), recSum:q('.d-recsum'), recBody:q('.d-recbody'),
    logSum:q('.d-logsum'), logPre:q('.d-logpre'),
  };
  const prev={};
  const setText=(el,key,val)=>{ if(el && prev[key]!==val){ prev[key]=val; el.textContent=val; } };
  const setHTML=(el,key,val)=>{ if(el && prev[key]!==val){ prev[key]=val; el.innerHTML=val; } };

  function update(a){
    const st=a.status||{}, color=STATE_COLOR[st.state]||'var(--muted)', c=a.counts, tg=a.timings;
    const dotc=a.alive?'var(--accent)':'var(--bad)';
    if(prev.dot!==dotc){ prev.dot=dotc; refs.dot.style.background=dotc; }
    if(view!=='list') setText(refs.devpill,'dev', a.device+(a.pid?(' · pid '+a.pid):''));
    // status line — only rebuilt when its signature changes (keeps the live timer)
    const sig=stageSig(st);
    if(prev.sig!==sig){ prev.sig=sig; refs.state.innerHTML=stageLineHTML(st); refs.state.style.color=color; }
    setText(refs.posted,'posted', c.posted);
    if(view!=='list'){ setText(refs.confirmed,'confirmed', c.confirmed); setText(refs.unconf,'unconf', c.unconfirmed); }
    if(prev.failed!==c.failed){ prev.failed=c.failed; refs.failed.textContent=c.failed;
      refs.failed.style.color=c.failed?'var(--bad)':'inherit'; }
    setText(refs.avg,'avg', fmtDur(tg.total.avg));
    setText(refs.last,'last', a.last_post_ts?ago(a.last_post_ts):'—');
    setHTML(refs.stageAvg,'stageavg',
      `stage avg — prepare ${fmtDur(tg.prepare.avg)} · publish ${fmtDur(tg.publish.avg)} · verify ${fmtDur(tg.verify.avg)}`);
    setText(refs.recSum,'recsum', `recent posts (${(a.recent||[]).length})`);
    setHTML(refs.recBody,'recbody', recentRowsHTML(a));
    setText(refs.logSum,'logsum', `log (last ${(a.log||[]).length} lines)`);
    setText(refs.logPre,'logtext', (a.log||[]).join('\n')||'(empty)');
  }
  update(a);
  return {root, update, view};
}

const devNodes=new Map();    // account -> {root, update, view} — persistent nodes
let builtView=null;          // which view the #devs wrapper is currently built for

function ensureWrapper(view){
  const el=$('#devs');
  let container = view==='list'? el.querySelector('.devlist') : el.querySelector('.devgrid');
  if(builtView===view && container) return container;
  // view changed / first build / coming back from an empty state → rebuild
  // wrapper. Node cache stays valid only within the same view layout.
  if(builtView!==view) devNodes.clear();
  if(view==='list'){
    el.innerHTML='<div class="lhead">'+
      '<span></span><span>account / device</span><span>status</span>'+
      '<span class="lstat">posted</span><span class="lstat">failed</span>'+
      '<span class="lstat">avg</span><span class="lstat">last</span><span></span></div>'+
      '<div class="devlist"></div>';
    container=el.querySelector('.devlist');
  } else {
    el.innerHTML='<div class="devgrid"></div>';
    container=el.querySelector('.devgrid');
  }
  builtView=view;
  return container;
}

function renderDevs(d){
  const el=$('#devs'), pager=$('#pager');
  document.querySelectorAll('#viewtoggle button').forEach(b=>
    b.classList.toggle('on', b.dataset.view===VIEW));
  $('#fSizeWrap').style.display = VIEW==='list' ? '' : 'none';

  if(!d.accounts.length){
    el.innerHTML='<div class="muted">No devices in data/device_accounts.json</div>';
    builtView=null; devNodes.clear(); pager.innerHTML=''; $('#devcount').textContent=''; return;
  }
  // page size: list = selector (default 10); cards = exactly TWO rows of cards.
  const size = VIEW==='cards' ? cardsPerRow()*2 : (CTRL.size>0?CTRL.size:1e9);
  const rows=processAccounts(d.accounts);
  const pages=Math.max(1, Math.ceil(rows.length/size));
  if(CTRL.page>pages) CTRL.page=pages;
  if(CTRL.page<1) CTRL.page=1;
  const start=(CTRL.page-1)*size;
  const slice=rows.slice(start, start+size);

  $('#devcount').textContent = rows.length===d.accounts.length
    ? `(${d.accounts.length})` : `(${rows.length} of ${d.accounts.length})`;

  if(slice.length===0){
    el.innerHTML='<div class="muted">No devices match the current filter</div>';
    builtView=null;   // wrapper destroyed; rebuild on next non-empty render
  } else {
    const container=ensureWrapper(VIEW);
    // reuse a persistent node per account; only changed leaves are rewritten.
    const ordered=slice.map(a=>{
      let n=devNodes.get(a.account);
      if(!n || n.view!==VIEW){ n=buildDevNode(VIEW, a); devNodes.set(a.account, n); }
      else { n.update(a); }
      return n.root;
    });
    // replaceChildren moves existing nodes into the new order WITHOUT recreating
    // them — open <details> and scroll positions are preserved.
    container.replaceChildren(...ordered);
  }

  // pager (stateless; safe to rebuild)
  if(pages<=1){ pager.innerHTML=''; }
  else {
    pager.innerHTML =
      `<button id="pPrev" ${CTRL.page<=1?'disabled':''}>← prev</button>`+
      `<span>page ${CTRL.page} / ${pages} · showing ${start+1}–${start+slice.length} of ${rows.length}</span>`+
      `<button id="pNext" ${CTRL.page>=pages?'disabled':''}>next →</button>`;
    const pv=$('#pPrev'), nx=$('#pNext');
    if(pv) pv.onclick=()=>{ CTRL.page--; renderDevs(LAST); };
    if(nx) nx.onclick=()=>{ CTRL.page++; renderDevs(LAST); };
  }
}

function renderPosts(d){
  const rows=(d.session_posts||[]).map(p=>{
    const link=p.url?`<a href="${esc(p.url)}" target="_blank">link</a>`:'<span class="muted">—</span>';
    const vc=p.verdict==='SUCCESS'?'var(--accent)':'var(--warn)';
    return `<tr><td>${ago(p.ts)}</td><td>@${esc(p.account)}</td><td>${esc(p.name||'—')}</td>
      <td><span class="tag" style="color:${vc};border-color:${vc}">${esc(p.verdict)}</span></td>
      <td class="right">${fmtDur(p.total)}</td><td class="right">${link}</td></tr>`;
  }).join('') || '<tr><td colspan="6" class="muted">nothing posted this session yet</td></tr>';
  $('#posts').innerHTML=`<table><thead><tr><th>when</th><th>account</th><th>video</th>
    <th>verdict</th><th class="right">time</th><th class="right">link</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderFeed(d){
  const lines=(d.feed||[]).map(e=>{
    let extra=[e.account&&('@'+e.account), e.stage, e.name, e.verdict,
      e.seconds!=null&&fmtDur(e.seconds), e.reason].filter(Boolean).join(' ');
    return `${(e.ts||'').replace('T',' ').replace('Z','')}  ${(e.type||'').padEnd(13)} ${extra}`;
  }).join('\n');
  $('#feed').textContent=lines||'(no events)';
  $('#fleetlog').textContent=(d.fleet_log||[]).join('\n')||'(no supervisor log)';
}

function tickLive(){
  document.querySelectorAll('[data-live]').forEach(el=>{
    const t=Date.parse(el.getAttribute('data-live'));
    if(t) el.textContent=' '+fmtDur((Date.now()-t)/1000);
  });
  document.querySelectorAll('[data-until]').forEach(el=>{
    const t=Date.parse(el.getAttribute('data-until'));
    if(t){ const d=(t-Date.now())/1000; el.textContent = d>0 ? ' '+fmtDur(d) : ' now'; }
  });
}

let LAST=null;
document.querySelectorAll('#viewtoggle button').forEach(b=>b.addEventListener('click',()=>{
  VIEW=b.dataset.view; localStorage.setItem('fleetView',VIEW);
  if(LAST) renderDevs(LAST);
}));

// init control widgets from persisted state
$('#fSort').value=CTRL.sort; $('#fSize').value=String(CTRL.size); $('#fDir').textContent=CTRL.dir==='asc'?'▲':'▼';
const reRender=()=>{ if(LAST) renderDevs(LAST); };
$('#fSearch').addEventListener('input', e=>{ CTRL.search=e.target.value; CTRL.page=1; reRender(); });
$('#fState').addEventListener('change', e=>{ CTRL.state=e.target.value; CTRL.page=1; reRender(); });
$('#fAlive').addEventListener('change', e=>{ CTRL.alive=e.target.value; CTRL.page=1; reRender(); });
$('#fSort').addEventListener('change', e=>{ CTRL.sort=e.target.value; CTRL.page=1;
  localStorage.setItem('fleetSort',CTRL.sort); reRender(); });
$('#fSize').addEventListener('change', e=>{ CTRL.size=parseInt(e.target.value,10); CTRL.page=1;
  localStorage.setItem('fleetSize',String(CTRL.size)); reRender(); });
$('#fDir').addEventListener('click', ()=>{ CTRL.dir=CTRL.dir==='asc'?'desc':'asc';
  $('#fDir').textContent=CTRL.dir==='asc'?'▲':'▼'; localStorage.setItem('fleetDir',CTRL.dir); reRender(); });
// cards view = two rows: re-paginate when the column count can change
let _rsz; window.addEventListener('resize', ()=>{ clearTimeout(_rsz);
  _rsz=setTimeout(()=>{ if(LAST && VIEW==='cards') renderDevs(LAST); }, 150); });

async function refresh(){
  try{
    const r=await fetch('/api/state'); const d=await r.json();
    if(d.error){ $('#fleetState').textContent='error: '+d.error; return; }
    LAST=d;
    const f=d.fleet;
    $('#fleetState').innerHTML = f.stopped
      ? '<span class="dot" style="background:var(--bad)"></span>supervisor stopped'
      : (f.devices_active>0
         ? `<span class="dot" style="background:var(--accent)"></span>running · ${f.devices_active}/${f.devices_total} active`
         : '<span class="dot" style="background:var(--warn)"></span>idle');
    $('#clock').textContent='session since '+(f.session_start? f.session_start.replace('T',' ').replace('Z','') : '—');
    const sy=d.s3_sync;
    if(sy && sy.enabled){
      if(sy.last_error){
        $('#s3sync').innerHTML='<span class="dot" style="background:var(--bad)"></span>S3 sync error';
        $('#s3sync').title='S3 → fffbt.videos sync error: '+sy.last_error;
      }else{
        const color = sy.online ? 'var(--accent)' : 'var(--warn)';
        const age = (sy.age_sec==null) ? 'no sync yet' : 'synced '+fmtDur(sy.age_sec)+' ago';
        $('#s3sync').innerHTML='<span class="dot" style="background:'+color+'"></span>S3 sync · '+age;
        $('#s3sync').title=`S3 → fffbt.videos · ${sy.online?'online':'stale'} · `
          +`${sy.runs} runs · +${sy.inserted_total} rows total`;
      }
    }else{ $('#s3sync').textContent=''; }
    renderCards(d); renderStages(d); renderDevs(d); renderPosts(d); renderFeed(d);
    $('#refresh').textContent='⟳ '+new Date().toLocaleTimeString();
  }catch(e){ $('#fleetState').textContent='fetch failed'; }
}
// ---- Device control panel (separate view; own data source) ----------------
const selected = new Set();
let CTLLAST = null;
let CURTAB = localStorage.getItem('fleetTab') || 'control';
const taskNodes = new Map();       // task id -> {root, update} — persistent nodes
const TF = { status:'', cmd:'' };  // task filters
const CSTATE_COLOR = { device:'var(--accent)', offline:'var(--bad)',
  unauthorized:'var(--warn)', disconnected:'var(--muted)' };
const agoEpoch = sec => { if(!sec) return ''; const x=Date.now()/1000-sec;
  if(x<60) return Math.round(x)+'s ago'; if(x<3600) return Math.round(x/60)+'m ago';
  if(x<86400) return Math.round(x/3600)+'h ago'; return Math.round(x/86400)+'d ago'; };

function showTab(tab){
  CURTAB=tab; localStorage.setItem('fleetTab',tab);
  $('#controlView').style.display = tab==='control'?'':'none';
  $('#statsView').style.display   = tab==='stats'?'':'none';
  $('#proxyView').style.display   = tab==='proxy'?'':'none';
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on', b.dataset.tab===tab));
  if(tab==='control') refreshControl();
  if(tab==='proxy') refreshProxy();
}
document.querySelectorAll('#tabs button').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));

function updateSelCount(){
  $('#ctlSelCount').textContent = selected.size? `${selected.size} selected` : '';
  document.querySelectorAll('.ctl-actbtn').forEach(b=>b.disabled = selected.size===0);
}
function filteredDevices(){
  const all=CTLLAST?.devices||[];
  const q=($('#ctlSearch').value||'').trim().toLowerCase();
  const ros=$('#ctlRosterOnly').checked;
  const bf=($('#ctlBlockFilter')||{}).value||'all';
  const rf=($('#ctlStopFilter')||{}).value||'all';   // filter by last-run stop reason
  const out=all.filter(d=>{
    if(ros && !d.in_roster) return false;
    if(bf==='blocked' && !d.blocked) return false;
    if(bf==='unblocked' && d.blocked) return false;
    if(rf==='none' && d.stop_verdict) return false;            // only devices with no recorded run
    if(rf!=='all' && rf!=='none' && d.stop_verdict!==rf) return false;
    if(q && !((d.account||'').toLowerCase().includes(q) || d.serial.toLowerCase().includes(q))) return false;
    return true;
  });
  const sk=($('#ctlSort')||{}).value||'account';
  const keyf=d=> sk==='serial'?(d.serial||'') : sk==='state'?(d.state||'') :
                 sk==='stop'?(d.blocked?'!blocked':(d.stop_label||'~~~')) : (d.account||'~~~');
  out.sort((a,b)=> keyf(a).localeCompare(keyf(b),undefined,{numeric:true})
                   || (a.serial||'').localeCompare(b.serial||'',undefined,{numeric:true}));
  return out;
}
function renderDevicesView(){
  const cont=$('#ctlDevs'), all=CTLLAST?.devices||[], devs=filteredDevices();
  const have=new Set(all.map(d=>d.serial)), byser=new Map(all.map(d=>[d.serial,d]));
  // prune selections that vanished OR became busy (can't select a busy device)
  [...selected].forEach(s=>{ const dd=byser.get(s); if(!have.has(s) || dd?.busy) selected.delete(s); });
  const blk=CTLLAST?.blocked_count||0;
  $('#ctlDevCount').innerHTML = (devs.length===all.length? `(${all.length})` : `(${devs.length} of ${all.length})`)
    + (blk? ` · <span id="ctlBlkCount">⚠ ${blk} blocked</span>` : '');
  if(!devs.length){ cont.innerHTML='<div class="muted" style="padding:12px">no devices match</div>'; updateSelCount(); return; }
  const head='<div class="chead"><label class="hall"><input type="checkbox" id="ctlAllCk"> All</label>'
    +'<span>account</span><span>serial</span><span style="text-align:right">adb / state</span></div>';
  cont.innerHTML=head+devs.map(d=>{
    const col=CSTATE_COLOR[d.state]||'var(--muted)';
    const acct=d.account?('@'+esc(d.account)):'<span class="muted">— unbound</span>';
    // last-run stop reason (TRIAL_LIMIT / trial reels not enabled / repeated failures / …)
    // shown for non-blocked, not-currently-busy devices so an active account's last
    // outcome is visible; blocked shows its own ⚠ badge, busy shows the task.
    const fail=d.stop_verdict && !['SUCCESS','PUBLISHED_UNCONFIRMED'].includes(d.stop_verdict);
    const sub=(d.stop_label && !d.blocked && !d.busy)
      ? `<span class="cstop${fail?' bad':''}" title="last run: ${esc(d.stop_verdict||'')} · ${agoEpoch(d.stop_since)}">${esc(d.stop_label)}</span>` : '';
    // BLOCKED always shows (even if the device is still flagged busy in a task), so a
    // real block is never hidden behind the busy badge. A blocked device is selectable.
    const bb=d.blocked?`<span class="bblk" title="${esc(d.block_reason||'login challenge')} · ${agoEpoch(d.block_since)}">⚠ BLOCKED</span> `:'';
    let cell;
    if(d.busy && !d.blocked){
      const lab=d.busy.length>18?d.busy.slice(0,18)+'…':d.busy;
      cell=`<span class="cstate"><span class="bbusy" title="busy: ${esc(d.busy)}">⏳ ${esc(lab)}</span></span>`;
    } else {
      cell=`<span class="cstate" style="color:${col}">${bb}${esc(d.state)}</span>`;
    }
    const lock = d.busy && !d.blocked;          // blocked -> not locked, can reassign
    return `<label class="crow${lock?' busy':(d.blocked?' blk':'')}">
      <input type="checkbox" data-ser="${esc(d.serial)}" ${selected.has(d.serial)?'checked':''} ${lock?'disabled':''}>
      <span class="cacct">${acct}${sub}</span><span class="cser">${esc(d.serial)}</span>${cell}</label>`;
  }).join('');
  cont.querySelectorAll('.crow input[type=checkbox]').forEach(cb=>cb.addEventListener('change',e=>{
    const s=e.target.dataset.ser; e.target.checked?selected.add(s):selected.delete(s); updateSelCount();
  }));
  // header "All" -> toggle all SELECTABLE (non-busy) devices in the current filter
  const allck=$('#ctlAllCk');
  if(allck){
    const selectable=devs.filter(d=>!d.busy);
    const nSel=selectable.filter(d=>selected.has(d.serial)).length;
    allck.checked = selectable.length>0 && nSel===selectable.length;
    allck.indeterminate = nSel>0 && nSel<selectable.length;
    allck.addEventListener('change',()=>{
      if(allck.checked) selectable.forEach(d=>selected.add(d.serial));
      else devs.forEach(d=>selected.delete(d.serial));
      renderDevicesView();
    });
  }
  updateSelCount();
}
function taskStatusKey(t){ return t.running?'active':(t.rc===0?'success':'failed'); }
function filterTasks(tasks){
  return (tasks||[]).filter(t=>{
    if(TF.status && taskStatusKey(t)!==TF.status) return false;
    if(TF.cmd && t.action!==TF.cmd) return false;
    return true;
  });
}
// Persistent node per task: only changed leaf VALUES are rewritten, so an open
// <details> log (and its scroll) survives every 3s refresh.
function buildTaskNode(t){
  const root=document.createElement('div'); root.className='task';
  root.innerHTML=
    `<div class="thead"><span class="t-name"></span><span class="t-meta mini"></span>
       <span class="grow"></span><span class="t-stop"></span></div>
     <div class="mini t-status" style="margin:5px 0"></div>
     <details><summary class="mini t-logsum">log</summary><pre class="log t-log"></pre></details>`;
  const q=s=>root.querySelector(s);
  const r={name:q('.t-name'),meta:q('.t-meta'),stop:q('.t-stop'),status:q('.t-status'),
           logsum:q('.t-logsum'),log:q('.t-log')};
  const prev={};
  const setT=(el,k,v)=>{ if(prev[k]!==v){ prev[k]=v; el.textContent=v; } };
  const setH=(el,k,v)=>{ if(prev[k]!==v){ prev[k]=v; el.innerHTML=v; } };
  function update(t){
    setT(r.name,'name',t.label);
    const devs=t.devices.length>3?`${t.devices.length} devices`:t.devices.join(', ');
    setT(r.meta,'meta',`· ${devs} · pid ${t.pid}`);
    const nact=(t.active_devices||t.devices||[]).length, ndev=(t.devices||[]).length;
    const col=t.stopping?'var(--warn)':(t.running?'var(--info)':(t.rc===0?'var(--accent)':'var(--bad)'));
    const txt=t.stopping?`stopping… (finishing in-flight) · ${fmtDur(t.runtime)}`
             :t.running?`running · ${fmtDur(t.runtime)} · ${nact}/${ndev} active`
                       :`finished · rc=${t.rc==null?'?':t.rc} · ran ${fmtDur(t.runtime)}`;
    setH(r.status,'status',`<span style="color:${col}">${t.running?'<span class="spin">⏱</span> ':''}${esc(txt)}</span>`);
    const sig=!t.running?'done':(t.stopping?'stopping':'run');
    if(prev.stopShown!==sig){
      prev.stopShown=sig;
      r.stop.innerHTML = !t.running ? '' :
        (t.stopping ? '<span class="mini" style="color:var(--warn)">⏳ stopping…</span>'
                    : '<button class="btn-stop">■ stop</button>');
      const b=r.stop.querySelector('button');
      if(b) b.onclick=async()=>{
        if(!confirm('Stop this task?\nIt stops claiming new videos and finishes any in-flight post (incl. verifying a live reel), then unclaims un-posted videos.')) return;
        b.disabled=true;
        await fetch('/api/control/stop',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:t.id})});
        refreshControl(); };
    }
    setT(r.logsum,'logsum',`log (last ${(t.log_tail||[]).length})`);
    setT(r.log,'logtext',(t.log_tail||[]).join('\n')||'(empty)');
  }
  update(t);
  return {root, update};
}
function renderTasks(tasksAll){
  const cont=$('#ctlTasks');
  const csel=$('#tfCmd');
  if(csel && !csel.dataset.built && (CTLLAST?.actions||[]).length){
    csel.innerHTML='<option value="">all commands</option>'
      +CTLLAST.actions.map(a=>`<option value="${esc(a.id)}">${esc(a.label)}</option>`).join('');
    csel.dataset.built='1';
  }
  const tasks=filterTasks(tasksAll);
  if(!tasks.length){
    cont.innerHTML='<div class="muted">'+((tasksAll||[]).length?'no tasks match the filter':'no tasks yet — pick devices and run')+'</div>';
    taskNodes.clear(); return;
  }
  const ordered=tasks.map(t=>{
    let n=taskNodes.get(t.id);
    if(!n){ n=buildTaskNode(t); taskNodes.set(t.id,n); } else n.update(t);
    return n.root;
  });
  cont.replaceChildren(...ordered);   // reorders existing nodes WITHOUT recreating them
  const ids=new Set(tasks.map(t=>t.id));
  [...taskNodes.keys()].forEach(k=>{ if(!ids.has(k)) taskNodes.delete(k); });
}
async function refreshControl(){
  try{
    const d=await (await fetch('/api/control/state')).json(); CTLLAST=d;
    const ab=$('#ctlActions');
    if(ab && !ab.dataset.built && d.actions){
      ab.innerHTML=d.actions.map(a=>`<button class="ctl-actbtn${a.id==='post'?' primary':''}${a.danger?' danger':''}" data-act="${esc(a.id)}">${esc(a.label)}</button>`).join('');
      ab.dataset.built='1';
      ab.querySelectorAll('.ctl-actbtn').forEach(b=>b.addEventListener('click',()=>onActionClick(b.dataset.act)));
      updateSelCount();
    }
    $('#ctlAdb').innerHTML=d.adb_ok
      ? '<span class="dot" style="background:var(--accent)"></span>adb ok'
      : '<span class="dot" style="background:var(--bad)"></span>adb unavailable';
    renderDevicesView(); renderTasks(d.tasks||[]);
  }catch(e){ $('#ctlAdb').textContent='control fetch failed'; }
}
function staggerVal(){ return parseInt($('#ctlStagger').value||'20',10); }
function onActionClick(act){
  if(selected.size===0){ alert('Select at least one device first.'); return; }
  if(act==='post'){ openPostModal(); return; }   // Post opens the count/loop modal
  runAction(act, {stagger: staggerVal()});
}
function blockedInSelection(act){
  if(act==='verify'||act==='discover') return 0;
  return (CTLLAST?.devices||[]).filter(d=>selected.has(d.serial)&&d.blocked).length;
}
function runAction(act, opts){
  const devices=[...selected]; if(!devices.length) return;
  const meta=(CTLLAST?.actions||[]).find(a=>a.id===act)||{};
  const nb=blockedInSelection(act);
  const blk=nb?`⚠ ${nb} of these are BLOCKED (login challenge) — they'll stop immediately.\n`:'';
  const m=act==='recover'?'This REBOOTS the devices. ':'';
  if((meta.danger||blk) && !confirm(`"${meta.label||act}" on ${devices.length} device(s).\n${blk}${m}Continue?`)) return;
  postRun(act, devices, opts);
}
async function postRun(act, devices, opts){
  try{
    const j=await (await fetch('/api/control/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:act, devices, ...opts})})).json();
    if(!j.ok) alert('Failed: '+(j.error||'unknown'));
  }catch(e){ alert('request failed'); }
  refreshControl();
}
function updatePmCat(){
  const cats=CTLLAST?.categories||[];
  const c=cats.find(x=>x.category===$('#pmCat').value);
  $('#pmCatN').textContent = c?`${c.new} videos available (status=new)`:'no new videos';
}
function openPostModal(){
  $('#pmCount').textContent=`${selected.size} device(s) selected`;
  const cats=CTLLAST?.categories||[], sel=$('#pmCat'), prevv=sel.value;
  sel.innerHTML = cats.length
    ? cats.map(c=>`<option value="${esc(c.category)}">${esc(c.category)} (${c.new} new)</option>`).join('')
    : '<option value="trend">trend</option>';
  if(prevv && cats.some(c=>c.category===prevv)) sel.value=prevv;
  updatePmCat();
  $('#postModal').style.display='flex';
}
function closePostModal(){ $('#postModal').style.display='none'; }
$('#pmCancel').addEventListener('click', closePostModal);
$('#postModal').addEventListener('click', e=>{ if(e.target.id==='postModal') closePostModal(); });
$('#pmCat').addEventListener('change', updatePmCat);
$('#pmStart').addEventListener('click', ()=>{
  const devices=[...selected]; if(!devices.length){ closePostModal(); return; }
  const loop=document.querySelector('input[name=pmmode]:checked').value==='loop';
  const order=document.querySelector('input[name=pmorder]:checked').value;
  const cat=$('#pmCat').value||'trend';
  const dmin=$('#pmDmin').value||'15', dmax=$('#pmDmax').value||'45';
  const opts={ stagger: staggerVal(), loop, category: cat, order,
    count: parseInt($('#pmN').value||'1',10),
    delay_min: Math.round(parseFloat(dmin)*60), delay_max: Math.round(parseFloat(dmax)*60),
    max_24h: parseInt($('#pmCap').value||'20',10) };
  const nb=blockedInSelection('post');
  const blk=nb?`⚠ ${nb} of ${devices.length} are BLOCKED — they'll stop immediately.\n`:'';
  const what=loop?'loop continuously':`${opts.count} reel(s) each`;
  const ord=order==='desc'?'newest first':'oldest first';
  if(!confirm(`Post ${what} from "${cat}" (${ord}) on ${devices.length} device(s) — REAL reels.\n${blk}delay ${dmin}-${dmax} min · 24h cap ${opts.max_24h||'off'}. Continue?`)) return;
  closePostModal();
  postRun('post', devices, opts);
});
$('#ctlReload').addEventListener('click', refreshControl);
$('#ctlClear').addEventListener('click', async e=>{ e.preventDefault();
  await fetch('/api/control/clear',{method:'POST'}); refreshControl(); });
$('#tfStatus').addEventListener('change', e=>{ TF.status=e.target.value; renderTasks(CTLLAST?.tasks||[]); });
$('#tfCmd').addEventListener('change', e=>{ TF.cmd=e.target.value; renderTasks(CTLLAST?.tasks||[]); });
$('#ctlSearch').addEventListener('input', renderDevicesView);
$('#ctlRosterOnly').addEventListener('change', renderDevicesView);
$('#ctlBlockFilter').addEventListener('change', renderDevicesView);
$('#ctlSort').addEventListener('change', renderDevicesView);
$('#ctlStopFilter').addEventListener('change', renderDevicesView);
$('#ctlSelAll').addEventListener('click',e=>{ e.preventDefault();
  filteredDevices().forEach(d=>{ if(!d.busy) selected.add(d.serial); }); renderDevicesView(); });
$('#ctlSelNone').addEventListener('click',e=>{ e.preventDefault(); selected.clear(); renderDevicesView(); });
$('#ctlSelOnline').addEventListener('click',e=>{ e.preventDefault();
  filteredDevices().forEach(d=>{ if(d.state==='device' && !d.busy) selected.add(d.serial); }); renderDevicesView(); });
$('#ctlSelRoster').addEventListener('click',e=>{ e.preventDefault();
  (CTLLAST?.devices||[]).forEach(d=>{ if(d.in_roster && !d.busy) selected.add(d.serial); }); renderDevicesView(); });
$('#ctlSelBlocked').addEventListener('click',e=>{ e.preventDefault();
  (CTLLAST?.devices||[]).forEach(d=>{ if(d.blocked && !d.busy) selected.add(d.serial); }); renderDevicesView(); });
// ---- Proxy tab ----
let PXLAST=null;
const pxSel=new Map();   // idproxy -> provider
const PX_COLOR={ ok:'var(--accent)', expiring:'var(--warn)', expired:'var(--bad)',
  external:'var(--muted)', none:'var(--muted)' };
function fmtLeft(h){ if(h==null) return '—'; if(h<0) return 'expired';
  if(h<24) return h.toFixed(1)+'h'; return (h/24).toFixed(1)+'d'; }
function pxFiltered(){
  let items=(PXLAST&&PXLAST.items)||[];
  if($('#pxManagedOnly').checked) items=items.filter(i=>i.renewable);
  if($('#pxWorkOnly').checked) items=items.filter(i=>i.in_work);
  return items;
}
async function refreshProxy(){
  let d; try{ d=await (await fetch('/api/proxy/state')).json(); }catch(e){ return; }
  PXLAST=d; renderProxy();
}
function renderProxy(){
  const s=(PXLAST&&PXLAST.summary)||{}, ro=PXLAST&&PXLAST.router_ok;
  $('#pxCards').innerHTML=[
    card('Devices', s.devices||0, ro?'router online':'⚠ router offline', ro?'':'var(--bad)'),
    card('Managed', s.managed||0, 'in our proxy.vn account', 'var(--info)'),
    card('Healthy', ((s.devices||0)-(s.unhealthy||0)), `${s.unhealthy||0} unhealthy`, s.unhealthy?'var(--warn)':'var(--accent)'),
    card('Expiring <24h', s.expiring||0, `${s.expired||0} expired`, (s.expiring||s.expired)?'var(--warn)':'var(--accent)'),
    card('Auto-renew due', s.due||0, 'in-work & expiring', s.due?'var(--warn)':'var(--muted)'),
    card('External', s.external||0, 'not in our account', 'var(--muted)'),
  ].join('');
  const items=pxFiltered();
  $('#pxRows').innerHTML = items.map(i=>{
    const px=i.proxy?`${esc(i.proxy.server||'?')}:${i.proxy.port||''}`:'<span class="muted">none</span>';
    const hc = i.health.working===true?'var(--accent)':(i.health.working===false?'var(--bad)':'var(--muted)');
    const ht = i.health.working===true?`✓ ${i.health.latency_ms||''}ms`:(i.health.working===false?'✗ down':'—');
    const cb = i.renewable?`<input type="checkbox" class="pxcb" data-id="${i.idproxy}" data-prov="${esc(i.provider||'')}" ${pxSel.has(i.idproxy)?'checked':''}/>`:'';
    const rb = i.renewable?`<button class="dirbtn pxrenew" data-id="${i.idproxy}" data-prov="${esc(i.provider||'')}">renew</button>`:'';
    const rot = i.ip?`<button class="dirbtn pxrotate" data-ip="${esc(i.ip)}" title="buy a fresh proxy & assign it (replaces current)">rotate</button>`:'';
    const acct = i.account?('@'+esc(i.account)):'<span class="muted">—</span>';
    const blk = i.blocked?' <span class="tag" style="color:var(--bad);border-color:var(--bad)">BLOCKED</span>':'';
    return `<tr>
      <td>${cb}</td><td>${esc(i.ip)}</td><td>${acct}${blk}</td>
      <td class="mini">${px}</td><td>${esc(i.provider||'—')}</td>
      <td class="right" style="color:${PX_COLOR[i.state]||''}">${fmtLeft(i.hours_left)}</td>
      <td style="color:${hc}">${ht}</td>
      <td><span class="tag" style="color:${PX_COLOR[i.state]||'var(--muted)'};border-color:${PX_COLOR[i.state]||'var(--muted)'}">${esc(i.state)}</span></td>
      <td style="white-space:nowrap">${rb} ${rot}</td></tr>`;
  }).join('') || '<tr><td colspan="9" class="muted">no devices</td></tr>';
  $('#pxSelCount').textContent = pxSel.size?`${pxSel.size} selected`:'';
}
async function pxRenew(items){
  if(!items.length) return;
  if(!confirm(`Renew ${items.length} proxy(ies) for 2 days — this SPENDS money. Continue?`)) return;
  $('#pxMsg').textContent='renewing…';
  const r=await (await fetch('/api/proxy/renew',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})})).json();
  const ok=(r.results||[]).filter(x=>x.ok).length;
  $('#pxMsg').textContent=`renewed ${ok}/${items.length}`; pxSel.clear(); refreshProxy();
}
async function pxRotate(ip){
  if(!ip) return;
  const prov=$('#pxBuyProv').value;
  if(!confirm(`Rotate proxy on ${ip}: buy a NEW ${prov} proxy (2 days, SPENDS money) and assign it, replacing the current one. Continue?`)) return;
  $('#pxMsg').textContent=`rotating ${ip}…`;
  const r=await (await fetch('/api/proxy/rotate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:ip,provider:prov})})).json();
  $('#pxMsg').textContent = r.ok ? `rotated ${ip} → ${r.result.proxy} (working=${r.result.working})` : ('rotate error: '+(r.error||(r.result&&r.result.error)));
  refreshProxy();
}
$('#pxRows').addEventListener('click', e=>{
  const rb=e.target.closest('.pxrenew'); if(rb){ pxRenew([{idproxy:+rb.dataset.id, provider:rb.dataset.prov}]); return; }
  const ro=e.target.closest('.pxrotate'); if(ro){ pxRotate(ro.dataset.ip); return; } });
$('#pxRows').addEventListener('change', e=>{ const cb=e.target.closest('.pxcb'); if(!cb) return;
  const id=+cb.dataset.id; if(cb.checked) pxSel.set(id, cb.dataset.prov); else pxSel.delete(id);
  $('#pxSelCount').textContent = pxSel.size?`${pxSel.size} selected`:''; });
$('#pxAll').addEventListener('change', e=>{ pxFiltered().forEach(i=>{ if(i.renewable){
  if(e.target.checked) pxSel.set(i.idproxy,i.provider||''); else pxSel.delete(i.idproxy); } }); renderProxy(); });
$('#pxRenewSel').addEventListener('click', ()=> pxRenew([...pxSel].map(([id,prov])=>({idproxy:id,provider:prov}))));
$('#pxReload').addEventListener('click', refreshProxy);
$('#pxManagedOnly').addEventListener('change', renderProxy);
$('#pxWorkOnly').addEventListener('change', renderProxy);
$('#pxSync').addEventListener('click', async ()=>{ $('#pxMsg').textContent='syncing…';
  const r=await (await fetch('/api/proxy/sync',{method:'POST'})).json();
  $('#pxMsg').textContent=r.ok?`synced ${r.synced}`:('sync error: '+r.error); refreshProxy(); });
$('#pxBuy').addEventListener('click', async ()=>{
  const prov=$('#pxBuyProv').value, count=+$('#pxBuyCount').value||1, days=+$('#pxBuyDays').value||30;
  if(!confirm(`Buy ${count} ${prov} proxy(ies) for ${days} days — this SPENDS money. Continue?`)) return;
  $('#pxMsg').textContent='buying…';
  const r=await (await fetch('/api/proxy/buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider:prov,count,days})})).json();
  $('#pxMsg').textContent=r.ok?`bought ${r.bought}, stored ${r.stored}`:('buy error: '+r.error); refreshProxy(); });

showTab(CURTAB);
setInterval(()=>{ if(CURTAB==='control') refreshControl(); }, 3000);
setInterval(()=>{ if(CURTAB==='proxy') refreshProxy(); }, 8000);

refresh();
setInterval(refresh, 3000);
setInterval(tickLive, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
