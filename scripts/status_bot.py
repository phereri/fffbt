#!/usr/bin/env python3
"""Telegram status bot for the Trial-Reel posting fleet.

Posts ONE message into a chat and keeps EDITING it to show live status:

  * device roster split into active / idle / blocked / offline;
  * remaining backlog in fffbt.videos + estimated runway at current rate;
  * age of the last post (liveness) and content-age of the last posted clip;
  * 24h throughput, lifetime total posted, error rate;
  * the list of accounts Instagram is currently challenging (blocked).

Design (see the design discussion): this is a thin POLLER. It reuses the
already-running dashboard's JSON endpoints for the hard, event-derived fleet
state (active/blocked/idle, throughput, error rate, backlog) and runs ONE
extra Supabase Management-API query for the few durable numbers the dashboard
does not expose (24h count, lifetime total, last published_at + filename for
the two "age" lines). The live dashboard is never modified or restarted.

Data sources:
  GET http://127.0.0.1:8765/api/state          build_state() blob
  GET http://127.0.0.1:8765/api/control/state  control_state() blob
  Supabase Management API                       durable post counts (one query)

Telegram delivery is stdlib-only (urllib) so there are no new deps. The bot
keeps {chat_id, message_id, offset} in data/tg_status_msg.json. Bootstrap by
sending /bind in the target chat (works even with bot privacy mode ON, since
commands are always delivered). After that the bot only edits its one message.

Run:
  python scripts/status_bot.py                  # daemon: poll + edit forever
  python scripts/status_bot.py --dry            # render once to stdout, no Telegram
  python scripts/status_bot.py --once           # one edit cycle then exit (cron)

Env (.env):
  TELEGRAM_BOT_TOKEN     required
  TELEGRAM_STATUS_CHAT   optional; pre-bind a chat id (skips /bind)
  FLEET_DASH_HOST/PORT   dashboard location (default 127.0.0.1:8765)
  STATUS_REFRESH_SECS    edit cadence (default 30)
  SUPABASE_PROJECT_REF / SUPABASE_PAT   for the durable-counts query
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path

# Windows consoles default to cp1252 and choke on the emoji we render; force
# UTF-8 so --dry output and log lines never raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "tg_status_msg.json"

DASH_HOST = os.environ.get("FLEET_DASH_HOST", "127.0.0.1")
DASH_PORT = os.environ.get("FLEET_DASH_PORT", "8765")
DASH_BASE = f"http://{DASH_HOST}:{DASH_PORT}"
REFRESH_SECS = int(os.environ.get("STATUS_REFRESH_SECS", "30"))

# Health thresholds (tunable).
STALE_RED_MIN = 30      # no post for >30 min -> red
STALE_YELLOW_MIN = 15   # >15 min -> yellow
RUNWAY_YELLOW_HR = 6    # backlog runway under this -> yellow
ERROR_YELLOW_PCT = 15   # error rate over this -> yellow
BLOCK_YELLOW_PCT = 20   # blocked share over this -> yellow
OFFLINE_YELLOW_PCT = 15 # offline share over this -> yellow

# Pace model (capacity-based). темп = active_devices_now * (1 / cadence), where
# cadence is the MEASURED median interval between two consecutive posts of the
# same account (not the duration of a single post). This gives a realistic number
# the instant the fleet starts (cadence comes from history) and tracks the live
# active count immediately — a dropped account lowers темп without waiting for a
# 24h average to catch up. The recent window is blended onto the historical prior
# as today's posts accrue, so the estimate self-corrects toward current reality.
CAD_MAX_GAP_MIN = 240     # ignore gaps longer than this: overnight / trial-limit sleep, not active cadence
CAD_RECENT_HR = 6         # "today's run" measurement window
CAD_PRIOR_DAYS = 7        # historical prior window
CAD_MIN_SAMPLE = 8        # min gaps before a window's cadence is trusted
CAD_BLEND_TARGET_N = 40   # recent posts needed to fully trust today over the prior
CAD_DEFAULT_MIN = 120     # cold-start cadence when there is no history at all


# ---------------------------------------------------------------------------
# .env + tiny utilities (inlined to keep the bot import-light and stdlib-only)
# ---------------------------------------------------------------------------
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


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fmt(n) -> str:
    """12430 -> '12 430' (Russian thousands grouping)."""
    try:
        return f"{int(n):,}".replace(",", " ")  # narrow no-break space
    except (TypeError, ValueError):
        return "—"


def _parse_pg_ts(ts: str | None) -> float | None:
    """Parse a Supabase/Postgres timestamptz string to an epoch float."""
    if not ts:
        return None
    s = str(ts).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # '+00' -> '+00:00'
    m = re.search(r"([+-]\d{2})$", s)
    if m:
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        # last resort: strip fractional + tz and assume UTC
        try:
            base = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
            if base:
                return datetime.strptime(base.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc).timestamp()
        except Exception:
            pass
    return None


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    if s < 60:
        return "только что"
    m = s // 60
    if m < 60:
        return f"{m} мин назад"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} ч {m} мин назад" if m else f"{h} ч назад"
    d = h // 24
    return f"{d} дн назад"


def _age_days_str(shot: date | None) -> str:
    if shot is None:
        return "—"
    d = (date.today() - shot).days
    if d <= 0:
        return "сегодня"
    return f"{d} дн"


def _shot_date_from_name(name: str | None) -> date | None:
    """First 8-digit run in the filename is the VID_YYYYMMDD shoot date."""
    if not name:
        return None
    m = re.search(r"\d{8}", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%Y%m%d").date()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Supabase Management API (durable post counts) — one query
# ---------------------------------------------------------------------------
def _mgmt_query(sql: str, timeout: int = 25) -> list[dict] | None:
    ref = os.environ.get("SUPABASE_PROJECT_REF", "")
    pat = os.environ.get("SUPABASE_PAT", "")
    if not ref or not pat:
        return None
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                 "User-Agent": "fffbt-status-bot/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else None
    except Exception:
        return None


_DB_SQL = f"""
WITH g AS (
  SELECT published_at AS ts,
         EXTRACT(EPOCH FROM (published_at - lag(published_at)
           OVER (PARTITION BY posted_by ORDER BY published_at))) AS gap_s
  FROM fffbt.videos
  WHERE platform='Instagram' AND status IN ('posted','verify')
    AND published_at IS NOT NULL AND posted_by IS NOT NULL
    AND published_at > now() - interval '{CAD_PRIOR_DAYS} days'
),
gg AS (
  SELECT ts, gap_s FROM g WHERE gap_s > 0 AND gap_s < {CAD_MAX_GAP_MIN * 60}
),
cad AS (
  SELECT
    percentile_cont(0.25) WITHIN GROUP (ORDER BY gap_s) AS p25_7d,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY gap_s) AS p50_7d,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY gap_s) AS p75_7d,
    count(*) AS n_7d,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY gap_s)
      FILTER (WHERE ts > now() - interval '{CAD_RECENT_HR} hours') AS p25_6h,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY gap_s)
      FILTER (WHERE ts > now() - interval '{CAD_RECENT_HR} hours') AS p50_6h,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY gap_s)
      FILTER (WHERE ts > now() - interval '{CAD_RECENT_HR} hours') AS p75_6h,
    count(*) FILTER (WHERE ts > now() - interval '{CAD_RECENT_HR} hours') AS n_6h
  FROM gg
)
SELECT
  (SELECT count(*) FROM fffbt.videos WHERE platform='Instagram'
     AND status IN ('posted','verify') AND published_at > now() - interval '24 hours') AS posted_24h,
  (SELECT count(*) FROM fffbt.videos WHERE platform='Instagram'
     AND status IN ('posted','verify')) AS total_posted,
  (SELECT count(*) FROM fffbt.videos WHERE platform='Instagram'
     AND status='new') AS remaining_new,
  (SELECT max(published_at) FROM fffbt.videos WHERE platform='Instagram'
     AND published_at IS NOT NULL) AS last_published_at,
  (SELECT name FROM fffbt.videos WHERE platform='Instagram'
     AND published_at IS NOT NULL ORDER BY published_at DESC LIMIT 1) AS last_name,
  cad.p25_7d, cad.p50_7d, cad.p75_7d, cad.n_7d,
  cad.p25_6h, cad.p50_6h, cad.p75_6h, cad.n_6h
FROM cad
""".strip()

_db_cache: dict = {"ts": 0.0, "data": None}


def db_extras() -> dict | None:
    now = time.time()
    if _db_cache["data"] is not None and now - _db_cache["ts"] < 60:
        return _db_cache["data"]
    rows = _mgmt_query(_DB_SQL)
    if not rows:
        _db_cache.update(ts=now, data=None)
        return None
    r = rows[0]

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    out = {
        "posted_24h": int(r.get("posted_24h") or 0),
        "total_posted": int(r.get("total_posted") or 0),
        "remaining_new": int(r.get("remaining_new") or 0),
        "last_published_at": r.get("last_published_at"),
        "last_name": r.get("last_name"),
        "cad": {
            "p25_7d": _f(r.get("p25_7d")), "p50_7d": _f(r.get("p50_7d")),
            "p75_7d": _f(r.get("p75_7d")), "n_7d": int(r.get("n_7d") or 0),
            "p25_6h": _f(r.get("p25_6h")), "p50_6h": _f(r.get("p50_6h")),
            "p75_6h": _f(r.get("p75_6h")), "n_6h": int(r.get("n_6h") or 0),
        },
    }
    _db_cache.update(ts=now, data=out)
    return out


# ---------------------------------------------------------------------------
# dashboard JSON
# ---------------------------------------------------------------------------
def _dash_get(path: str, timeout: int = 15) -> dict | None:
    try:
        with urllib.request.urlopen(f"{DASH_BASE}{path}", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def classify(state: dict, control: dict | None) -> dict:
    """Partition the roster into active / idle / blocked / offline (sums to total)."""
    accounts = state.get("accounts") or []
    total = (state.get("fleet") or {}).get("devices_total") or len(accounts)

    control_ok = bool(control and control.get("devices"))
    blocked_accts: set[str] = set()
    adb_state: dict[str, str] = {}
    if control_ok:
        for d in control["devices"]:
            acct = d.get("account")
            if acct and d.get("blocked"):
                blocked_accts.add(acct)
            if acct and d.get("state"):
                adb_state[acct] = d["state"]

    OFFLINE = {"disconnected", "offline", "unauthorized"}
    active = idle = offline = blocked = 0
    for a in accounts:
        acct = a.get("account")
        if acct in blocked_accts:
            blocked += 1
        elif a.get("alive"):
            active += 1
        elif adb_state.get(acct) in OFFLINE:
            offline += 1
        else:
            idle += 1

    return {
        "total": total,
        "active": active,
        "idle": idle,
        "offline": offline,
        "blocked": blocked,
        "blocked_names": sorted(blocked_accts),
        "control_ok": control_ok,
    }


def _fleet_last_post_ts(state: dict) -> float | None:
    """Newest last_post_ts across accounts (event-based fallback for age)."""
    best = None
    for a in state.get("accounts") or []:
        t = _parse_ts_iso(a.get("last_post_ts"))
        if t and (best is None or t > best):
            best = t
    return best


def _parse_ts_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return _parse_pg_ts(ts)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _health(counts: dict, last_post_age_s: float | None, runway_hr: float | None,
            error_rate: float, dash_ok: bool) -> tuple[str, str]:
    """Return (emoji, label). Worst condition wins."""
    if not dash_ok:
        return "🔴", "дашборд недоступен"
    total = counts["total"] or 1
    reasons_red, reasons_yellow = [], []
    if counts["active"] == 0:
        reasons_red.append("флот стоит")
    if last_post_age_s is not None and last_post_age_s > STALE_RED_MIN * 60:
        reasons_red.append(f"нет постов >{STALE_RED_MIN}м")
    elif last_post_age_s is not None and last_post_age_s > STALE_YELLOW_MIN * 60:
        reasons_yellow.append(f"нет постов >{STALE_YELLOW_MIN}м")
    if runway_hr is not None and runway_hr < RUNWAY_YELLOW_HR:
        reasons_yellow.append(f"запас <{RUNWAY_YELLOW_HR}ч")
    if not counts.get("control_ok"):
        # control endpoint down: blocked/active/idle split is unreliable, say so
        reasons_yellow.append("контроль недоступен")
    elif counts["blocked"] > total * BLOCK_YELLOW_PCT / 100:
        reasons_yellow.append("много блоков")
    if error_rate > ERROR_YELLOW_PCT:
        reasons_yellow.append(f"ошибки {error_rate}%")
    if counts["offline"] > total * OFFLINE_YELLOW_PCT / 100:
        reasons_yellow.append(f"офлайн {counts['offline']}")

    if reasons_red:
        return "🔴", "стоп · " + ", ".join(reasons_red)
    if reasons_yellow:
        return "🟡", "внимание · " + ", ".join(reasons_yellow)
    return "🟢", "работает"


def _blend_cad(p_recent, n_recent, p_prior, n_prior, default_s) -> float:
    """Cadence (seconds) blending the recent window onto the 7-day prior.

    Weight grows with the number of recent samples, so at fleet startup (no posts
    yet today) we use the historical prior and get an instant realistic number,
    then drift toward today's measured cadence as posts accrue. Falls back
    prior -> default when a window is too thin to trust.
    """
    prior = p_prior if (p_prior and n_prior >= CAD_MIN_SAMPLE) else default_s
    if p_recent and n_recent >= 1:
        w = min(1.0, n_recent / float(CAD_BLEND_TARGET_N))
        return prior * (1.0 - w) + p_recent * w
    return prior


def _estimate_pace(active: int, remaining, cad: dict | None) -> dict:
    """Capacity-based pace: темп = active * (1 / cadence).

    Returns the central rate (posts/hr) plus a runway range in hours derived from
    the p25..p75 cadence spread (the 15-vs-30-min / wait-time variability). A short
    gap (p25) = fast rate = shorter runway; a long gap (p75) = slow rate = longer
    runway.
    """
    default_s = CAD_DEFAULT_MIN * 60.0
    if not cad:
        c25 = default_s * 0.7
        c50 = default_s
        c75 = default_s * 1.5
        n_recent = 0
    else:
        n_recent = cad.get("n_6h") or 0
        c25 = _blend_cad(cad["p25_6h"], n_recent, cad["p25_7d"], cad["n_7d"], default_s * 0.7)
        c50 = _blend_cad(cad["p50_6h"], n_recent, cad["p50_7d"], cad["n_7d"], default_s)
        c75 = _blend_cad(cad["p75_6h"], n_recent, cad["p75_7d"], cad["n_7d"], default_s * 1.5)
    # Blending two windows can invert the order slightly; keep p25<=p50<=p75.
    c25, c50, c75 = sorted([c25, c50, c75])

    def rate(cs):  # posts/hour at current active count
        return active * 3600.0 / cs if (active and cs and cs > 0) else 0.0

    rate_mid, rate_fast, rate_slow = rate(c50), rate(c25), rate(c75)

    def runway(rt):  # hours of backlog at this rate
        if not remaining or not rt or rt <= 0:  # no backlog / no rate -> undefined
            return None
        return remaining / rt

    return {
        "rate_mid": rate_mid,
        "runway_mid": runway(rate_mid),
        "runway_low": runway(rate_fast),    # fast rate -> shortest runway
        "runway_high": runway(rate_slow),   # slow rate -> longest runway
        "projected": n_recent < CAD_MIN_SAMPLE,  # ~no data yet today -> pure forecast from history
    }


def _fmt_rate(r) -> str:
    if not r:
        return "—"
    return f"{round(r)}" if r >= 10 else f"{r:.1f}"


def _fmt_runway_range(low_h, high_h) -> str:
    """Render a runway range, picking ч / дн by the high end. Collapses to a single
    value when both ends round equal; caps at >30 дн."""
    if low_h is None or high_h is None:
        return "—"
    lo, hi = sorted([low_h, high_h])
    if hi >= 48:
        lo_d, hi_d = lo / 24.0, hi / 24.0
        if hi_d > 30:
            return ">30 дн"
        if round(lo_d, 1) == round(hi_d, 1):
            return f"~{hi_d:.1f} дн"
        return f"{lo_d:.1f}–{hi_d:.1f} дн"
    lo_h, hi_h = round(lo), round(hi)
    return f"~{hi_h} ч" if lo_h == hi_h else f"{lo_h}–{hi_h} ч"


def build_message() -> str:
    state = _dash_get("/api/state")
    now_local = datetime.now().strftime("%H:%M:%S")

    if not state or "error" in state:
        return (
            "📊 <b>FFFBT — статус выкладки</b>\n"
            f"🕒 {now_local} · 🔴 дашборд недоступен\n\n"
            f"Не могу получить данные с дашборда (<code>{html.escape(DASH_BASE)}</code>).\n"
            "Проверь, что fleet_dashboard.py запущен."
        )

    control = _dash_get("/api/control/state")
    extras = db_extras()
    counts = classify(state, control)
    summary = state.get("summary") or {}
    backlog = state.get("backlog") or {}

    # remaining backlog: prefer the authoritative DB count of status='new';
    # fall back to the dashboard's backlog blob only if the DB query failed.
    remaining = (extras or {}).get("remaining_new")
    if remaining is None:
        remaining = backlog.get("new")
    posted_24h = (extras or {}).get("posted_24h")
    total_posted = (extras or {}).get("total_posted")
    error_rate = summary.get("error_rate") or 0

    # capacity-based pace: темп = active_now * (1 / measured cadence), with a runway
    # range from the cadence spread (see _estimate_pace). The midpoint feeds health.
    pace = _estimate_pace(counts["active"], remaining, (extras or {}).get("cad"))
    runway_hr = pace["runway_mid"]

    # last-post age (prefer durable DB published_at; fall back to events)
    last_pub = _parse_pg_ts((extras or {}).get("last_published_at")) or _fleet_last_post_ts(state)
    last_post_age = (time.time() - last_pub) if last_pub else None
    shot = _shot_date_from_name((extras or {}).get("last_name"))

    emoji, label = _health(counts, last_post_age, runway_hr, error_rate, dash_ok=True)

    L = []
    L.append("📊 <b>FFFBT — статус выкладки</b>")
    L.append(f"🕒 {now_local} · {emoji} {html.escape(label)}")
    L.append("")
    L.append(f"🤖 <b>Устройства — {counts['total']}</b>")
    L.append(f"   ▶️ активно — <b>{counts['active']}</b>")
    L.append(f"   💤 простой — <b>{counts['idle']}</b>")
    L.append(f"   ⛔ блок — <b>{counts['blocked'] if counts.get('control_ok') else '—'}</b>")
    L.append(f"   ✖️ офлайн — <b>{counts['offline']}</b>")
    L.append("")
    L.append("🎬 <b>Видео в БД</b>")
    L.append(f"   🆕 осталось — <b>{fmt(remaining) if remaining is not None else '—'}</b>")
    L.append(f"   🕐 запас — <b>{_fmt_runway_range(pace['runway_low'], pace['runway_high'])}</b>")
    L.append(f"   ⏱ посл. пост — <b>{_ago(last_post_age)}</b>")
    L.append(f"   📅 возраст ролика — <b>{_age_days_str(shot)}</b>")
    L.append("")
    L.append("📈 <b>Статистика</b>")
    L.append(f"   ✅ за 24ч — <b>{fmt(posted_24h) if posted_24h is not None else '—'}</b>")
    if pace["rate_mid"]:
        L.append(f"   ⚡ темп — <b>{_fmt_rate(pace['rate_mid'])} /ч</b>{' · прогноз' if pace['projected'] else ''}")
    else:
        L.append("   ⚡ темп — <b>—</b>")
    L.append(f"   🗂 всего — <b>{fmt(total_posted) if total_posted is not None else '—'}</b>")
    L.append(f"   ✖️ ошибки — <b>{error_rate} %</b>")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------
TOKEN = ""


def _tg(method: str, http_timeout: int = 20, **params) -> dict:
    """Call a Telegram Bot API method.

    NOTE: the socket timeout is ``http_timeout`` — NOT ``timeout`` — so that a
    ``timeout=`` kwarg flows through ``**params`` into the request body (that is
    the getUpdates long-poll seconds). On HTTP 429 we honor ``retry_after`` with
    a single bounded retry so we don't escalate Telegram's flood-wait.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"

    def _once() -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(params).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code}", "error_code": e.code}
        except Exception as e:
            return {"ok": False, "description": str(e)}

    r = _once()
    if not r.get("ok"):
        retry_after = (r.get("parameters") or {}).get("retry_after")
        if retry_after and not _stop.is_set():
            wait = min(int(retry_after) + 1, 60)
            print(f"[status_bot] 429 from {method}; sleeping {wait}s", flush=True)
            _stop.wait(wait)
            r = _once()
    return r


# shared, persisted state
_lock = threading.Lock()          # guards reads/writes of _state fields
_send_lock = threading.Lock()     # serializes the whole edit/send network op
_save_lock = threading.Lock()     # serializes the atomic state-file write
_state = {"chat_id": None, "message_id": None, "offset": 0, "last_text": None}
_stop = threading.Event()


def _load_state() -> None:
    d = _read_json(STATE_FILE)
    for k in ("chat_id", "message_id", "offset"):
        if d.get(k) is not None:
            _state[k] = d[k]
    env_chat = os.environ.get("TELEGRAM_STATUS_CHAT")
    if env_chat and not _state["chat_id"]:
        try:
            _state["chat_id"] = int(env_chat)
        except ValueError:
            _state["chat_id"] = env_chat


def _save_state() -> None:
    """Atomic, serialized write so a crash mid-write can't truncate the file."""
    with _lock:
        snap = {k: _state[k] for k in ("chat_id", "message_id", "offset")}
    with _save_lock:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.parent / (STATE_FILE.name + ".tmp")
            tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, STATE_FILE)  # atomic rename on the same filesystem
        except Exception as e:
            print(f"[status_bot] state save failed: {e}", flush=True)


def _pin(chat, mid) -> None:
    """Best-effort pin (needs the 'pin messages' admin right). Logs on failure
    so a missing right is visible instead of silently leaving it unpinned."""
    r = _tg("pinChatMessage", chat_id=chat, message_id=mid, disable_notification=True)
    if not r.get("ok"):
        print(f"[status_bot] pin skipped: {r.get('description')}", flush=True)


def _send_or_edit(text: str) -> None:
    # _send_lock makes the whole read-decide-network-write sequence atomic so two
    # threads (refresh tick + a /bind|/refresh command) can never both create a
    # new message or both edit the same one (TOCTOU double-send / double-edit).
    with _send_lock:
        with _lock:
            chat = _state["chat_id"]
            mid = _state["message_id"]
            last = _state["last_text"]
        if not chat or text == last:
            return

        if mid:
            r = _tg("editMessageText", chat_id=chat, message_id=mid, text=text,
                    parse_mode="HTML", disable_web_page_preview=True)
            if r.get("ok"):
                with _lock:
                    _state["last_text"] = text
                return
            desc = (r.get("description") or "").lower()
            if "not modified" in desc:
                with _lock:
                    _state["last_text"] = text
                return
            # message gone / uneditable -> fall through and re-send a fresh one
            if not any(k in desc for k in ("not found", "can't be edited",
                                           "message to edit", "message_id")):
                print(f"[status_bot] edit failed: {desc}", flush=True)
                return

        r = _tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                disable_web_page_preview=True)
        if r.get("ok"):
            new_id = r["result"]["message_id"]
            with _lock:
                _state["message_id"] = new_id
                _state["last_text"] = text
            _save_state()
            _pin(chat, new_id)
        else:
            print(f"[status_bot] send failed: {r.get('description')}", flush=True)


def push(force: bool = False) -> None:
    if force:
        with _lock:
            _state["last_text"] = None
    try:
        _send_or_edit(build_message())
    except Exception as e:
        print(f"[status_bot] render/push error: {e}", flush=True)


# ---------------------------------------------------------------------------
# command handling (/bind, /unbind, /refresh)
# ---------------------------------------------------------------------------
def _handle_update(u: dict) -> None:
    msg = u.get("message") or u.get("channel_post") or {}
    text = (msg.get("text") or "").strip()
    chat = (msg.get("chat") or {}).get("id")
    if not text.startswith("/") or chat is None:
        return
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/bind":
        with _lock:
            _state["chat_id"] = chat
            _state["message_id"] = None
            _state["last_text"] = None
        _save_state()
        print(f"[status_bot] bound to chat {chat}", flush=True)
        push(force=True)
    elif cmd == "/unbind":
        with _lock:
            _state["chat_id"] = None
            _state["message_id"] = None
            _state["last_text"] = None
        _save_state()
        print("[status_bot] unbound", flush=True)
    elif cmd in ("/refresh", "/status"):
        push(force=True)


_POLL_SECS = 30  # getUpdates server-side long-poll seconds


def _updates_loop() -> None:
    while not _stop.is_set():
        # `timeout` is the Telegram long-poll param (flows via **params); the
        # socket timeout must exceed it so urlopen doesn't cut the poll short.
        r = _tg("getUpdates", http_timeout=_POLL_SECS + 15, timeout=_POLL_SECS,
                offset=_state["offset"], allowed_updates=["message", "channel_post"])
        if not r.get("ok"):
            retry_after = (r.get("parameters") or {}).get("retry_after")
            _stop.wait(max(3, int(retry_after) + 1) if retry_after else 3)
            continue
        results = r.get("result") or []
        for u in results:
            _state["offset"] = u["update_id"] + 1
            try:
                _handle_update(u)
            except Exception as e:
                print(f"[status_bot] update error: {e}", flush=True)
        if results:
            _save_state()


def _refresh_loop() -> None:
    while not _stop.is_set():
        push(force=False)
        _stop.wait(REFRESH_SECS)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    global TOKEN
    ap = argparse.ArgumentParser(description="Telegram fleet status bot")
    ap.add_argument("--dry", action="store_true",
                    help="render once to stdout, no Telegram")
    ap.add_argument("--once", action="store_true",
                    help="one edit cycle then exit (cron-style)")
    args = ap.parse_args(argv)

    _load_env()
    _load_state()
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    if args.dry:
        print(build_message())
        return 0

    if not TOKEN:
        print("[status_bot] TELEGRAM_BOT_TOKEN is not set in .env", file=sys.stderr)
        return 2

    if args.once:
        if not _state["chat_id"]:
            print("[status_bot] no chat bound; send /bind first (or set "
                  "TELEGRAM_STATUS_CHAT)", file=sys.stderr)
            return 3
        push(force=True)
        return 0

    # Clean shutdown on Ctrl-C AND on SIGTERM (systemd/Docker stop), so the
    # refresh loop wakes immediately and the final offset/state is flushed.
    def _on_signal(signum, _frame):
        _stop.set()
    for _sig in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, _sig), _on_signal)
        except Exception:
            pass

    print(f"[status_bot] up · dashboard={DASH_BASE} · refresh={REFRESH_SECS}s · "
          f"chat={_state['chat_id'] or '(unbound; send /bind)'}", flush=True)
    upd = threading.Thread(target=_updates_loop, name="tg-updates", daemon=True)
    upd.start()
    try:
        _refresh_loop()
    except KeyboardInterrupt:
        _stop.set()
    finally:
        _save_state()  # flush the latest offset before exit
        print("[status_bot] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
