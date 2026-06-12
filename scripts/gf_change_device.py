"""Run GenFarmer ChangeDevice headlessly via the local Automation REST API.

App(ChangeInfo) -> Task(bind device) -> Run -> execute -> poll status+logs ->
verify the phone is still reachable on adb (the documented hazard is that the
phone goes fully offline with no remote recovery).

⚠️ DESTRUCTIVE + HAZARDOUS. Requires --yes AND a physically-recoverable phone.
Run ON the Windows host (it talks to 127.0.0.1:55554) via run_with_env.ps1 +
the venv python, e.g.:  gf_change_device.py <serial> --yes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:55554"
APP_NAME = "ChangeDeviceApp"  # resolved to an appId from the DB (override with --app-id)
DB = r"C:\Users\Administrator\.genfarmer\db.sqlite"


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def _id(resp):
    d = resp.get("data", resp)
    return d.get("id") if isinstance(d, dict) else None


def device_triple(serial: str) -> dict:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT name, current_device_id, serial_no FROM devices WHERE current_device_id=?",
        (serial,),
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"device {serial} not found in GenFarmer DB")
    return {"id": row["current_device_id"], "serialNo": row["serial_no"], "name": row["name"]}


def resolve_app_id(name: str) -> str:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("SELECT id FROM apps WHERE name=? ORDER BY rowid DESC LIMIT 1", (name,)).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"app named {name!r} not found in GenFarmer DB")
    return row[0]


def fingerprint(serial: str) -> dict:
    """Read the identity fields ChangeDevice may mutate, for a before/after diff."""
    adb = os.environ.get("ADB_BIN", "adb")
    props = {
        "ro.serialno": None, "ro.product.model": None, "ro.build.fingerprint": None,
        "ro.boot.serialno": None, "gsm.sim.imei": None, "ro.ril.oem.imei": None,
    }
    out = {}
    for p in props:
        r = subprocess.run([adb, "-s", serial, "shell", "getprop", p], capture_output=True, text=True, timeout=15)
        out[p] = r.stdout.strip()
    for key, cmd in (("android_id", ["settings", "get", "secure", "android_id"]),
                     ("gaid", ["settings", "get", "secure", "advertising_id"])):
        r = subprocess.run([adb, "-s", serial, "shell", *cmd], capture_output=True, text=True, timeout=15)
        out[key] = r.stdout.strip()
    return out


def adb_state(serial: str) -> str:
    adb = os.environ.get("ADB_BIN", "adb")
    try:
        subprocess.run([adb, "connect", serial], capture_output=True, timeout=12)
        r = subprocess.run([adb, "-s", serial, "get-state"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "offline"


def android_major(serial: str) -> int:
    """Spoofed Android version (ro.build.version.release), as an int major."""
    adb = os.environ.get("ADB_BIN", "adb")
    try:
        r = subprocess.run([adb, "-s", serial, "shell", "getprop", "ro.build.version.release"],
                           capture_output=True, text=True, timeout=10)
        m = re.match(r"\s*(\d+)", r.stdout.strip())
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def wait_reconnect(serial: str, secs: int = 300) -> bool:
    """A real change reboots the phone; always-on VPN reconnects in ~90s."""
    end = time.time() + secs
    while time.time() < end:
        if adb_state(serial) == "device":
            return True
        time.sleep(5)
    return False


def do_one_change(uid, app_id, triple) -> str:
    task = api("POST", "/automation/tasks", {
        "appId": app_id, "input": [], "userId": uid,
        "name": f"ChangeDevice-{triple['name']}",
        "devices": {"enable": True, "list": [triple]},
    })
    task_id = _id(task)
    run = api("POST", "/automation/runs", {
        "userId": uid, "taskId": task_id, "appId": app_id, "status": 0,
    })
    run_id = _id(run)
    api("PUT", f"/automation/runs/{run_id}/run")
    for _ in range(40):  # poll until terminal
        time.sleep(3)
        status = (api("GET", f"/automation/runs/{run_id}").get("data") or {}).get("status")
        if status in (3, 4, 5):
            break
    return run_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("serial", help="e.g. 100.91.90.9:5555")
    ap.add_argument("--yes", action="store_true", help="actually run (DESTRUCTIVE/HAZARDOUS)")
    ap.add_argument("--user-id", type=int, default=None)
    ap.add_argument("--app-id", default=None, help="override; else resolved from app name")
    ap.add_argument("--min-android", type=int, default=12,
                    help="retry ChangeDevice until the new identity is >= this Android version (default 12)")
    ap.add_argument("--max-attempts", type=int, default=6)
    ap.add_argument("--trigger-only", action="store_true",
                    help="fire one ChangeDevice and exit (for manual/collaborative reconnect)")
    args = ap.parse_args()

    me = api("GET", "/backend/auth/me")
    uid = args.user_id or me.get("data", {}).get("id")
    app_id = args.app_id or resolve_app_id(APP_NAME)
    triple = device_triple(args.serial)
    print(f"userId={uid}  appId={app_id}  device={triple}")
    print(f"before: adb state={adb_state(args.serial)!r}  android={android_major(args.serial)}")
    fp_before = fingerprint(args.serial)
    print("fingerprint BEFORE:", json.dumps(fp_before))

    if not args.yes:
        print("\nDRY RUN — pass --yes to execute (ensure the phone can be PHYSICALLY recoverable).")
        return 0

    if args.trigger_only:
        run_id = do_one_change(uid, app_id, triple)
        print(f"TRIGGERED run {run_id}. Phone will reboot + drop Tailscale shortly — re-open the "
              f"Tailscale app on the phone. Re-check version once it's back.")
        return 0

    for attempt in range(1, args.max_attempts + 1):
        print(f"\n--- ChangeDevice attempt {attempt}/{args.max_attempts} ---", flush=True)
        run_id = do_one_change(uid, app_id, triple)
        print(f"run {run_id} done; waiting for reconnect (reboot + always-on VPN)...", flush=True)
        if not wait_reconnect(args.serial):
            print("⚠️ PHONE UNREACHABLE after ChangeDevice — needs recovery (see hazard runbook).")
            return 2
        ver = android_major(args.serial)
        model = subprocess.run([os.environ.get("ADB_BIN", "adb"), "-s", args.serial, "shell",
                                "getprop", "ro.product.model"], capture_output=True, text=True, timeout=10).stdout.strip()
        print(f"  -> reconnected: model={model!r} android={ver}")
        if ver >= args.min_android:
            print(f"OK — Android {ver} >= {args.min_android}.")
            fp_after = fingerprint(args.serial)
            print("fingerprint AFTER:", json.dumps(fp_after))
            changed = {k: (fp_before.get(k), fp_after.get(k)) for k in fp_after if fp_before.get(k) != fp_after.get(k)}
            print("CHANGED FIELDS:", json.dumps(changed) if changed else "(none)")
            return 0
        print(f"  android {ver} < {args.min_android} — re-changing...", flush=True)
    print(f"⚠️ could not reach Android >= {args.min_android} in {args.max_attempts} attempts.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
