#!/usr/bin/env python3
"""Unattended Trial-Reel posting loop for one phone.

Posts one reel via ``scripts/post_trial.py``, then sleeps a random 15-45 min and
repeats. Self-heals the MobileRun a11y "not available" case (toggle + reboot)
and writes clear ESCALATE lines for conditions a human must see. Designed to run
in the background; a monitor watches the log for ESCALATE.

Status lines (grep-able):
  LOOP START / POST_OK / POST_UNCONFIRMED / POST_FAIL / RECOVER / SLEEP /
  ESCALATE / LOOP STOP
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.runner import fleet_events  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEVICE = os.environ.get("LOOP_DEVICE", "192.168.4.225:5555")
ACCOUNT = os.environ.get("LOOP_ACCOUNT", "linhduongil769")
CATEGORY = os.environ.get("LOOP_CATEGORY", "trend")
LOG_PATH = os.environ.get("LOOP_LOG", str(ROOT / "post_loop.log"))
VENV_PY = os.environ.get("LOOP_PY", str(ROOT / ".venv" / "Scripts" / "python.exe"))

MIN_DELAY = int(os.environ.get("LOOP_MIN_DELAY", 15 * 60))
MAX_DELAY = int(os.environ.get("LOOP_MAX_DELAY", 45 * 60))
POST_TIMEOUT = int(os.environ.get("LOOP_POST_TIMEOUT", 1800))   # 30 min hard cap
FAIL_RETRY_DELAY = int(os.environ.get("LOOP_FAIL_RETRY_DELAY", 90))  # short settle between failed retries (NOT the 15-45m cadence)
MAX_CONSEC_FAIL = int(os.environ.get("LOOP_MAX_CONSEC_FAIL", 5))
MAX_REBOOTS_PER_HOUR = int(os.environ.get("LOOP_MAX_REBOOTS", 3))

# Account safety: never exceed this many posts in a rolling 24h window.
MAX_POSTS_24H = int(os.environ.get("LOOP_MAX_24H", 20))
RATE_RECHECK_SECS = int(os.environ.get("LOOP_RATE_RECHECK", 15 * 60))

# Account-level hard stops a human must resolve — stop the loop.
HARD_STOP_CODES = {"logged_out", "action_blocked", "account_suspended", "login_challenge"}
A11Y_MARK = "Accessibility service not available"
MOBILERUN_IME = "com.mobilerun.portal/com.mobilerun.portal.service.MobilerunAccessibilityService"

ADB = os.environ.get("ADB_PATH", "adb")


def _load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _adb(*args, timeout=30):
    return subprocess.run([ADB, "-s", DEVICE, *args], capture_output=True, text=True, timeout=timeout)


def count_recent_posts():
    """Count this account's posts in the last 24h (status posted or verify).

    Returns the int count, or None if the query fails (caller must NOT block
    posting on a transient DB error).
    """
    ref = os.environ.get("SUPABASE_PROJECT_REF", "")
    pat = os.environ.get("SUPABASE_PAT", "")
    if not ref or not pat:
        return None
    sql = (
        "SELECT count(*) AS n FROM fffbt.videos WHERE posted_by="
        f"'{ACCOUNT}' AND status IN ('posted','verify') "
        "AND published_at > now() - interval '24 hours'"
    )
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                 "User-Agent": "fffbt-post-loop/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return int(data[0]["n"])
    except Exception as e:  # pragma: no cover - network/db dependent
        log(f"RATE_LIMIT count query failed: {e}")
        return None


def wait_for_rate_capacity():
    """Block until the trailing-24h post count is under MAX_POSTS_24H."""
    while True:
        n = count_recent_posts()
        if n is None:
            log("RATE_LIMIT count unavailable; proceeding without blocking")
            return
        if n < MAX_POSTS_24H:
            if n >= MAX_POSTS_24H - 3:
                log(f"RATE_LIMIT {n}/{MAX_POSTS_24H} posts in last 24h (near cap)")
            return
        log(f"RATE_LIMIT {n}/{MAX_POSTS_24H} posts in last 24h — pausing "
            f"{RATE_RECHECK_SECS // 60} min until capacity frees")
        until = (datetime.now(timezone.utc) + timedelta(seconds=RATE_RECHECK_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fleet_events.emit("rate_limit", account=ACCOUNT, device=DEVICE,
                          count=n, cap=MAX_POSTS_24H, until=until)
        time.sleep(RATE_RECHECK_SECS)


def post_once():
    """Run one post_trial; return (rc, code, a11y_seen)."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(
            [VENV_PY, str(ROOT / "scripts" / "post_trial.py"),
             "--device", DEVICE, "--account", ACCOUNT, "--category", CATEGORY],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=POST_TIMEOUT,
        )
        out = (p.stdout or "") + (p.stderr or "")
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if hasattr(e, "stdout") else ""
        log(f"POST_FAIL post_trial timed out after {POST_TIMEOUT}s")
        return 1, "TIMEOUT", (A11Y_MARK in (out or ""))
    code = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith('"code":'):
            val = s.split(":", 1)[1].strip().rstrip(",").strip()
            code = None if val == "null" else val.strip('"')
    return rc, code, (A11Y_MARK in out)


def recover_device() -> bool:
    """Toggle the Mobilerun a11y service off->on, reboot, wait for it to return."""
    log("RECOVER toggling a11y + rebooting device")
    fleet_events.emit("recover", account=ACCOUNT, device=DEVICE, state="start")
    try:
        _adb("shell", "settings", "delete", "secure", "enabled_accessibility_services")
        _adb("shell", "settings", "put", "secure", "accessibility_enabled", "0")
        time.sleep(2)
        _adb("shell", "settings", "put", "secure", "enabled_accessibility_services", MOBILERUN_IME)
        _adb("shell", "settings", "put", "secure", "accessibility_enabled", "1")
        _adb("reboot")
    except Exception as e:
        log(f"RECOVER reboot command failed: {e}")
        return False
    # WiFi adb devices do NOT auto-reconnect after a reboot — call
    # `adb connect <ip>` every 5s until the device reappears, up to 5 min.
    deadline = time.time() + 300
    back = False
    while time.time() < deadline:
        time.sleep(5)
        try:
            subprocess.run([ADB, "connect", DEVICE], capture_output=True, text=True, timeout=12)
            st = subprocess.run([ADB, "-s", DEVICE, "get-state"], capture_output=True, text=True, timeout=15)
            if st.stdout.strip() == "device":
                back = True
                break
        except Exception:
            pass
    if not back:
        log("RECOVER device did not reconnect within 5 min")
        return False
    for _ in range(48):
        try:
            b = _adb("shell", "getprop", "sys.boot_completed", timeout=10).stdout.strip()
            if b == "1":
                break
        except Exception:
            pass
        time.sleep(5)
    time.sleep(30)  # let Portal rebind a11y + TCP
    log("RECOVER device back online")
    fleet_events.emit("recover", account=ACCOUNT, device=DEVICE, state="done", ok=True)
    return True


def sleep_random(lo=MIN_DELAY, hi=MAX_DELAY):
    secs = random.randint(lo, hi)
    log(f"SLEEP {secs}s (~{secs // 60} min) until next post")
    until = (datetime.now(timezone.utc) + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fleet_events.emit("sleep", account=ACCOUNT, device=DEVICE, seconds=secs, until=until)
    time.sleep(secs)


def main() -> int:
    _load_env()
    global ADB
    ADB = os.environ.get("ADB_PATH", "adb")
    random.seed()
    log(f"LOOP START device={DEVICE} account={ACCOUNT} category={CATEGORY} "
        f"delay={MIN_DELAY}-{MAX_DELAY}s max_24h={MAX_POSTS_24H}")
    fleet_events.emit("loop_start", account=ACCOUNT, device=DEVICE, category=CATEGORY,
                      delay_min=MIN_DELAY, delay_max=MAX_DELAY, max_24h=MAX_POSTS_24H,
                      pid=os.getpid())

    consec_fail = 0
    reboot_times: list[float] = []

    while True:
        # never exceed the rolling-24h post cap (blocks here until capacity frees)
        wait_for_rate_capacity()

        rc, code, a11y = post_once()

        # The random 15-45 min delay spaces out *successful* posts only. A reel
        # that failed to publish wasted no slot, so we retry it right away — the
        # cadence wait applies after rc 0 (verified) or rc 2 (published, in
        # 'verify'); both mean a reel actually went live.
        if rc == 0:
            consec_fail = 0
            log("POST_OK reel posted + verified")
            sleep_random()
            continue
        if rc == 2:
            consec_fail = 0
            log("POST_UNCONFIRMED reel published but not confirmed (left in 'verify')")
            sleep_random()
            continue
        if rc == 3:
            log("ESCALATE no 'new'/trend videos left to post")
            fleet_events.emit("escalate", account=ACCOUNT, device=DEVICE,
                              reason="no 'new'/trend videos left to post")
            log("LOOP STOP")
            return 0

        # rc == 1 (or other) -> failure
        consec_fail += 1
        log(f"POST_FAIL rc={rc} code={code} a11y={a11y} consec={consec_fail}")
        fleet_events.emit("post_fail", account=ACCOUNT, device=DEVICE,
                          rc=rc, code=code, a11y=a11y, consec=consec_fail)

        if code in HARD_STOP_CODES:
            log(f"ESCALATE account hard-stop code={code} — human needed; stopping")
            fleet_events.emit("escalate", account=ACCOUNT, device=DEVICE,
                              reason=f"account hard-stop code={code}")
            log("LOOP STOP")
            return 0

        if a11y or code in ("INFRA", "TIMEOUT"):
            now = time.time()
            reboot_times = [t for t in reboot_times if now - t < 3600]
            if len(reboot_times) >= MAX_REBOOTS_PER_HOUR:
                log(f"ESCALATE a11y/infra failure but reboot cap reached "
                    f"({MAX_REBOOTS_PER_HOUR}/h); stopping")
                fleet_events.emit("escalate", account=ACCOUNT, device=DEVICE,
                                  reason=f"reboot cap reached ({MAX_REBOOTS_PER_HOUR}/h)")
                log("LOOP STOP")
                return 0
            reboot_times.append(now)
            if recover_device():
                consec_fail = 0
                log("SLEEP 60s after recovery")
                time.sleep(60)
                continue
            log("ESCALATE device recovery failed; stopping")
            fleet_events.emit("recover", account=ACCOUNT, device=DEVICE, state="done", ok=False)
            fleet_events.emit("escalate", account=ACCOUNT, device=DEVICE,
                              reason="device recovery failed")
            log("LOOP STOP")
            return 0

        if consec_fail >= MAX_CONSEC_FAIL:
            log(f"ESCALATE {consec_fail} consecutive failures (last code={code}); stopping")
            fleet_events.emit("escalate", account=ACCOUNT, device=DEVICE,
                              reason=f"{consec_fail} consecutive failures (last code={code})")
            log("LOOP STOP")
            return 0

        # No reel was posted -> do NOT spend the cadence wait; retry immediately.
        if FAIL_RETRY_DELAY:
            log(f"SLEEP {FAIL_RETRY_DELAY}s settle before immediate retry")
            time.sleep(FAIL_RETRY_DELAY)
        else:
            log("RETRY immediately (no reel posted, no cadence wait)")


if __name__ == "__main__":
    raise SystemExit(main())
