#!/usr/bin/env python3
"""Bridge: mirror the live account<->device roster into automation.accounts.

Phase 2 of account-as-entity. ``automation.accounts`` becomes the durable account
store: one row per discovered IG account, carrying its ``status`` and the device
(``bound_serial``) it is currently bound to. The local JSON roster
(``data/device_accounts.json``) stays the FAST hot-path cache that the runners
read at launch; every roster write best-effort-syncs into automation.accounts here
(a slow Management-API call must never sit on the posting hot path, only on the
rare explicit discover/bind).

Schema add (idempotent): automation.accounts.bound_serial text, bound_at timestamptz.
On insert, the password is back-filled from fffbt.accounts.uid where available
(only ~42/106 match today), '' otherwise — the live fleet does not use these
passwords yet, this is the migration seed.

CLI:
  python scripts/account_store.py --migrate     # add columns + sync current roster
  python scripts/account_store.py --sync        # sync current roster only
  python scripts/account_store.py --show        # print automation.accounts bindings
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINDING = ROOT / "data" / "device_accounts.json"


def _load_env() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _mgmt_query(sql: str):
    ref = os.environ.get("SUPABASE_PROJECT_REF")
    pat = os.environ.get("SUPABASE_PAT")
    if not ref or not pat:
        _load_env()   # standalone callers (discover_fleet) may not have loaded .env
        ref = os.environ.get("SUPABASE_PROJECT_REF")
        pat = os.environ.get("SUPABASE_PAT")
    if not ref or not pat:
        raise RuntimeError("SUPABASE_PROJECT_REF / SUPABASE_PAT not set")
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                 "User-Agent": "fffbt-account-store/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Management API {e.code}: {e.read().decode()[:300]}") from None


def ensure_schema() -> None:
    """Add the binding columns to automation.accounts if missing (idempotent)."""
    _mgmt_query(
        "ALTER TABLE automation.accounts "
        "ADD COLUMN IF NOT EXISTS bound_serial text, "
        "ADD COLUMN IF NOT EXISTS bound_at timestamptz")


def sync_roster(roster: dict) -> dict:
    """Mirror the FULL {serial->account} roster into automation.accounts: upsert
    each bound account (status defaults 'active', bound_serial=its device) and
    clear bound_serial for any account no longer bound. Idempotent. Best-effort:
    the caller wraps this in try/except so a DB hiccup never breaks a discover."""
    rows = [{"username": a, "serial": s} for s, a in (roster or {}).items()
            if a and isinstance(a, str)]
    if not rows:
        # NEVER mass-clear the durable store on an empty roster (a swallowed read
        # error or a post-reboot all-unreadable run must not wipe every binding).
        return {"upserted": 0, "cleared": 0, "skipped": "empty roster"}
    payload = json.dumps(rows).replace("'", "''")
    sql = f"""
    WITH input AS (
      SELECT username, serial FROM jsonb_to_recordset('{payload}'::jsonb)
        AS t(username text, serial text)
    ),
    cleared AS (
      UPDATE automation.accounts a SET bound_serial = NULL, updated_at = now()
      WHERE a.platform = 'instagram' AND a.bound_serial IS NOT NULL
        AND a.username NOT IN (SELECT username FROM input)
      RETURNING 1
    ),
    ups AS (
      INSERT INTO automation.accounts
        (username, platform, password, status, bound_serial, bound_at, is_validation)
      SELECT i.username, 'instagram',
             COALESCE((SELECT f.password FROM fffbt.accounts f
                       WHERE f.uid = i.username AND f.password IS NOT NULL
                         AND btrim(f.password) <> ''
                       ORDER BY f.updated_at DESC, f.id DESC LIMIT 1), ''),
             'active', i.serial, now(), false
      FROM input i
      ON CONFLICT (username, platform) DO UPDATE
        SET bound_serial = EXCLUDED.bound_serial, bound_at = now(), updated_at = now()
      RETURNING 1
    )
    SELECT (SELECT count(*) FROM ups) AS upserted,
           (SELECT count(*) FROM cleared) AS cleared;
    """
    r = _mgmt_query(sql)
    return r[0] if r and isinstance(r[0], dict) else {}


def bind_one(username: str, serial: str) -> None:
    """Upsert ONE discovered binding (used by the auto-discover path, which binds a
    single device). Frees ``serial`` from any other account first, so the
    one-device-one-account invariant holds in automation.accounts too."""
    u = str(username).replace("'", "''")
    s = str(serial).replace("'", "''")
    sql = f"""
    WITH freed AS (
      UPDATE automation.accounts SET bound_serial = NULL, updated_at = now()
      WHERE platform = 'instagram' AND bound_serial = '{s}' AND username <> '{u}'
      RETURNING 1
    )
    INSERT INTO automation.accounts
      (username, platform, password, status, bound_serial, bound_at, is_validation)
    VALUES ('{u}', 'instagram',
            COALESCE((SELECT password FROM fffbt.accounts WHERE uid = '{u}'
                      AND password IS NOT NULL AND btrim(password) <> ''
                      ORDER BY updated_at DESC, id DESC LIMIT 1), ''),
            'active', '{s}', now(), false)
    ON CONFLICT (username, platform) DO UPDATE
      SET bound_serial = EXCLUDED.bound_serial, bound_at = now(), updated_at = now();
    """
    _mgmt_query(sql)


def clear_binding(username: str) -> None:
    """Mark an account unbound (e.g. its device failed to re-read / was pruned)."""
    u = str(username).replace("'", "''")
    _mgmt_query("UPDATE automation.accounts SET bound_serial = NULL, updated_at = now() "
                f"WHERE platform = 'instagram' AND username = '{u}'")


def _roster() -> dict:
    try:
        return json.loads(BINDING.read_text(encoding="utf-8")).get("devices") or {}
    except Exception:
        return {}


def main(argv=None) -> int:
    _load_env()
    args = set(argv if argv is not None else sys.argv[1:])
    if "--migrate" in args:
        ensure_schema()
        print("schema: bound_serial / bound_at ensured")
    if "--migrate" in args or "--sync" in args:
        res = sync_roster(_roster())
        print(f"sync: upserted={res.get('upserted')} cleared={res.get('cleared')}")
    if "--show" in args or not args:
        rows = _mgmt_query("SELECT username, status, bound_serial, bound_at "
                           "FROM automation.accounts WHERE bound_serial IS NOT NULL "
                           "ORDER BY bound_at DESC NULLS LAST LIMIT 200")
        print(f"automation.accounts bound rows: {len(rows)}")
        for r in rows[:20]:
            print(f"  {r.get('username'):28} {r.get('status'):10} {r.get('bound_serial')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
