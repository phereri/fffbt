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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.runner import fleet_events  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"
PIDS_FILE = ROOT / "data" / "fleet_pids.json"
FLEET_LOG = ROOT / "post_fleet.log"

HOST = os.environ.get("FLEET_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("FLEET_DASH_PORT", "8765"))
LOG_TAIL_LINES = int(os.environ.get("FLEET_DASH_LOG_LINES", "40"))

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
        return {"state": "working", "stage": stage,
                "label": f"{stage}…", "since": since, "live_since": since}
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
        return {"state": "sleeping", "label": "sleeping until next post",
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
    return {"state": "unknown", "label": t or "?", "since": since}


def _account_block(account: str, device: str, all_events: list[dict],
                   session_start: float | None, win_pids: set[int] | None,
                   spawned_pids: dict[str, int]) -> dict:
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
    if not alive and status["state"] not in ("stopped", "done"):
        status = {"state": "offline", "label": "process not running", "since": status.get("since")}

    log_path = ROOT / f"post_loop_{_safe_account(account)}.log"
    return {
        "account": account,
        "device": device,
        "pid": pid,
        "alive": alive,
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
    accounts = [_account_block(acct, serial, all_events, session_start, win_pids, spawned_pids)
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
        },
        "stage_stats": {**agg_stage, "total": agg_total, "share": stage_share},
        "accounts": accounts,
        "session_posts": session_posts,
        "feed": list(reversed(feed)),
        "fleet_log": _tail(FLEET_LOG, 30),
        "backlog": _db_counts(),
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
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        return self._send(404, b"not found", "text/plain")


def main() -> int:
    _load_env()
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
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg);color:var(--fg)}
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
</style>
</head>
<body>
<header>
  <h1>📱 Fleet Dashboard</h1>
  <span id="fleetState" class="pill">…</span>
  <span class="grow"></span>
  <span class="muted" id="clock"></span>
  <span class="muted" id="refresh">⟳</span>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>

  <h2>Stage timing — what to optimise</h2>
  <div id="stages"></div>

  <h2>Devices</h2>
  <div class="devgrid" id="devs"></div>

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

function renderDevs(d){
  $('#devs').innerHTML = d.accounts.map(a=>{
    const st=a.status||{}, color=STATE_COLOR[st.state]||'var(--muted)';
    const liveStage = st.state==='working' && st.live_since;
    let stageLine = esc(st.label||'');
    if(liveStage) stageLine += ` <span class="spin">⏱</span><span data-live="${st.live_since}"></span>`;
    else if(st.until) stageLine += ` <span class="muted">(${until(st.until)})</span>`;
    else if(st.since) stageLine += ` <span class="muted">· ${ago(st.since)}</span>`;
    const c=a.counts, tg=a.timings;
    const recent = (a.recent||[]).slice().reverse().map(r=>{
      const link = r.url? `<a href="${esc(r.url)}" target="_blank">link</a>` : '<span class="muted">no link</span>';
      const vc = r.verdict==='SUCCESS'?'var(--accent)':'var(--warn)';
      return `<tr><td>${esc(r.name||'—')}</td>
        <td><span class="tag" style="color:${vc};border-color:${vc}">${esc(r.verdict||'')}</span></td>
        <td class="right">${fmtDur(r.total)}</td><td class="right">${ago(r.ts)}</td>
        <td class="right">${link}</td></tr>`;
    }).join('') || '<tr><td colspan="5" class="muted">no posts yet</td></tr>';
    return `<div class="dev">
      <div class="top">
        <span class="dot" style="background:${a.alive?'var(--accent)':'var(--bad)'}"></span>
        <span class="name">@${esc(a.account)}</span>
        <span class="grow"></span>
        <span class="pill mini">${esc(a.device)}${a.pid?(' · pid '+a.pid):''}</span>
      </div>
      <div class="body">
        <div style="color:${color};font-weight:600;margin-bottom:8px">${stageLine}</div>
        <div class="stat-row">
          <div><b style="color:var(--accent)">${c.posted}</b> <span class="mini">posted</span></div>
          <div><b>${c.confirmed}</b> <span class="mini">confirmed</span></div>
          <div><b style="color:var(--warn)">${c.unconfirmed}</b> <span class="mini">unconf.</span></div>
          <div><b style="color:${c.failed?'var(--bad)':'inherit'}">${c.failed}</b> <span class="mini">failed</span></div>
          <div><b>${fmtDur(tg.total.avg)}</b> <span class="mini">avg</span></div>
        </div>
        <div class="mini">stage avg — prepare ${fmtDur(tg.prepare.avg)} · publish ${fmtDur(tg.publish.avg)} · verify ${fmtDur(tg.verify.avg)}</div>
        <details><summary>recent posts (${(a.recent||[]).length})</summary>
          <table style="margin-top:6px"><tbody>${recent}</tbody></table>
        </details>
        <details><summary>log (last ${a.log.length} lines)</summary>
          <pre class="log">${esc((a.log||[]).join('\n'))||'(empty)'}</pre>
        </details>
      </div>
    </div>`;
  }).join('') || '<div class="muted">No devices in data/device_accounts.json</div>';
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
}

async function refresh(){
  try{
    const r=await fetch('/api/state'); const d=await r.json();
    if(d.error){ $('#fleetState').textContent='error: '+d.error; return; }
    const f=d.fleet;
    $('#fleetState').innerHTML = f.stopped
      ? '<span class="dot" style="background:var(--bad)"></span>supervisor stopped'
      : (f.devices_active>0
         ? `<span class="dot" style="background:var(--accent)"></span>running · ${f.devices_active}/${f.devices_total} active`
         : '<span class="dot" style="background:var(--warn)"></span>idle');
    $('#clock').textContent='session since '+(f.session_start? f.session_start.replace('T',' ').replace('Z','') : '—');
    renderCards(d); renderStages(d); renderDevs(d); renderPosts(d); renderFeed(d);
    $('#refresh').textContent='⟳ '+new Date().toLocaleTimeString();
  }catch(e){ $('#fleetState').textContent='fetch failed'; }
}
refresh();
setInterval(refresh, 3000);
setInterval(tickLive, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
