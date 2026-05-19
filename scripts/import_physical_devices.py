#!/usr/bin/env python3
"""Import a Tailscale device CSV export into automation.physical_devices.

Two transports are supported:

  1. Direct Postgres (default). Needs `SUPABASE_DB_URL` or `--db-url`.

       python scripts/import_physical_devices.py --csv <path> [--dry-run]

  2. Supabase Management API. Needs `SUPABASE_PAT` and `--project-ref`
     (or env `SUPABASE_PROJECT_REF`). Use this when no DB password is
     available — the PAT is a personal access token from
     https://supabase.com/dashboard/account/tokens .

       SUPABASE_PAT=sbp_... \\
       python scripts/import_physical_devices.py --csv <path> \\
           --via-management-api --project-ref <ref> [--dry-run]

The CSV is the standard Tailscale "Devices" export. Relevant columns:
    Device name, Device ID, OS, OS Version, Tailscale IPs, Last seen

For each row the script:
  - picks the IPv4 address out of "Tailscale IPs"
  - sets adb_connect_target = <ipv4>:5555
  - upserts on device_id (insert if new, update mutable fields otherwise)

The status of every imported device is set to 'offline'. The CSV is metadata
only; the device-environment agent flips a device to 'online' once it has
confirmed ADB reachability.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ADB_PORT = 5555

UPDATE_SQL = """
    UPDATE automation.physical_devices
    SET alias              = %(alias)s,
        device_name        = %(device_name)s,
        os                 = %(os)s,
        os_version         = %(os_version)s,
        tailscale_ipv4     = %(tailscale_ipv4)s,
        adb_connect_target = %(adb_connect_target)s,
        last_seen_at       = %(last_seen_at)s
    WHERE id = %(id)s
"""

INSERT_SQL = """
    INSERT INTO automation.physical_devices
        (alias, device_name, device_id, os, os_version,
         tailscale_ipv4, adb_connect_target, last_seen_at, status)
    VALUES
        (%(alias)s, %(device_name)s, %(device_id)s, %(os)s, %(os_version)s,
         %(tailscale_ipv4)s, %(adb_connect_target)s, %(last_seen_at)s, %(status)s)
"""


def extract_ipv4(tailscale_ips: str) -> str | None:
    """Pick the first IPv4 address from a comma-separated Tailscale IPs string."""
    for raw in (tailscale_ips or "").split(","):
        ip = raw.strip()
        # IPv4 has no colon; IPv6 always does. Tailscale exports both per device.
        if ip and ":" not in ip:
            return ip
    return None


def parse_timestamp(value: str) -> datetime | None:
    """Parse an RFC3339 Tailscale timestamp like '2026-05-12T14:39:53Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def row_to_record(row: dict) -> dict | None:
    device_id = (row.get("Device ID") or "").strip()
    device_name = (row.get("Device name") or "").strip()
    if not device_id or not device_name:
        return None

    ipv4 = extract_ipv4(row.get("Tailscale IPs", ""))
    return {
        "alias": device_name,
        "device_name": device_name,
        "device_id": device_id,
        "os": (row.get("OS") or "").strip() or "android",
        "os_version": (row.get("OS Version") or "").strip() or None,
        "tailscale_ipv4": ipv4,
        "adb_connect_target": f"{ipv4}:{ADB_PORT}" if ipv4 else None,
        "last_seen_at": parse_timestamp(row.get("Last seen", "")),
        "status": "offline",
    }


def upsert(conn, record: dict) -> str:
    """Insert or update a row matched by device_id. Returns 'insert' or 'update'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM automation.physical_devices WHERE device_id = %s",
            (record["device_id"],),
        )
        rows = cur.fetchall()
        if len(rows) > 1:
            # No unique constraint exists on device_id yet — bail rather than
            # update an arbitrary one of several matching rows.
            raise RuntimeError(
                f"{len(rows)} existing rows match device_id={record['device_id']!r}"
            )
        if rows:
            cur.execute(UPDATE_SQL, {**record, "id": rows[0][0]})
            return "update"
        cur.execute(INSERT_SQL, record)
        return "insert"


INPUTS_COLUMN_TYPES = (
    "alias text, device_name text, device_id text, os text, os_version text, "
    "tailscale_ipv4 text, adb_connect_target text, last_seen_at timestamptz, "
    "status text"
)


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"unserializable type: {type(o).__name__}")


def _management_api_query(project_ref: str, pat: str, sql: str) -> list[dict]:
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    body = json.dumps({"query": sql}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            # Cloudflare in front of api.supabase.com returns 403 / error code
            # 1010 to the default urllib User-Agent ("Python-urllib/3.x").
            "User-Agent": "fffbt-import-physical-devices/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Management API query failed ({e.code}): {detail}") from None
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected Management API response: {data!r}")
    return data


def run_via_management_api(
    records: list[dict], project_ref: str, pat: str, dry_run: bool
) -> int:
    json_payload = json.dumps(records, default=_json_default)
    sql_json = json_payload.replace("'", "''")
    inputs_cte = (
        f"inputs AS (\n"
        f"  SELECT * FROM jsonb_to_recordset('{sql_json}'::jsonb)\n"
        f"    AS t({INPUTS_COLUMN_TYPES})\n"
        f")"
    )

    dup_sql = f"""
        WITH {inputs_cte}
        SELECT pd.device_id AS device_id, count(*)::int AS c
        FROM automation.physical_devices pd
        JOIN inputs i ON i.device_id = pd.device_id
        GROUP BY pd.device_id
        HAVING count(*) > 1;
    """
    dups = _management_api_query(project_ref, pat, dup_sql)
    if dups:
        names = ", ".join(f"{r['device_id']!r} (x{r['c']})" for r in dups)
        raise RuntimeError(
            f"existing duplicate device_ids in target table: {names}"
        )

    if dry_run:
        classify_sql = f"""
            WITH {inputs_cte}
            SELECT i.device_id,
                   CASE WHEN EXISTS(
                       SELECT 1 FROM automation.physical_devices pd
                       WHERE pd.device_id = i.device_id
                   ) THEN 'update' ELSE 'insert' END AS action
            FROM inputs i
            ORDER BY action, device_id;
        """
        rows = _management_api_query(project_ref, pat, classify_sql)
    else:
        upsert_sql = f"""
            WITH {inputs_cte},
            updated AS (
              UPDATE automation.physical_devices pd
              SET alias              = i.alias,
                  device_name        = i.device_name,
                  os                 = i.os,
                  os_version         = i.os_version,
                  tailscale_ipv4     = i.tailscale_ipv4,
                  adb_connect_target = i.adb_connect_target,
                  last_seen_at       = i.last_seen_at
              FROM inputs i
              WHERE pd.device_id = i.device_id
              RETURNING pd.device_id
            ),
            inserted AS (
              INSERT INTO automation.physical_devices
                (alias, device_name, device_id, os, os_version,
                 tailscale_ipv4, adb_connect_target, last_seen_at, status)
              SELECT i.alias, i.device_name, i.device_id, i.os, i.os_version,
                     i.tailscale_ipv4, i.adb_connect_target, i.last_seen_at,
                     i.status
              FROM inputs i
              WHERE i.device_id NOT IN (SELECT device_id FROM updated)
              RETURNING device_id
            )
            SELECT device_id, 'update' AS action FROM updated
            UNION ALL
            SELECT device_id, 'insert' AS action FROM inserted
            ORDER BY action, device_id;
        """
        rows = _management_api_query(project_ref, pat, upsert_sql)

    counts = {"insert": 0, "update": 0}
    for r in rows:
        action = r["action"]
        counts[action] += 1
        print(f"{action}: device_id={r['device_id']}")
    prefix = "DRY RUN: " if dry_run else ""
    print(
        f"{prefix}{counts['insert']} insert(s), {counts['update']} update(s), "
        f"{len(records)} total."
    )
    return 0


def run_via_db_url(records: list[dict], db_url: str, dry_run: bool) -> int:
    import psycopg

    counts = {"insert": 0, "update": 0}
    conn = psycopg.connect(db_url)
    try:
        for rec in records:
            action = upsert(conn, rec)
            counts[action] += 1
            print(
                f"{action}: device_id={rec['device_id']} alias={rec['alias']} "
                f"ipv4={rec['tailscale_ipv4']} last_seen_at={rec['last_seen_at']}"
            )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    prefix = "DRY RUN: " if dry_run else ""
    print(
        f"{prefix}{counts['insert']} insert(s), {counts['update']} update(s), "
        f"{len(records)} total."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a Tailscale devices CSV into automation.physical_devices."
    )
    parser.add_argument("--csv", required=True, help="Path to the Tailscale devices CSV.")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("SUPABASE_DB_URL"),
        help="Postgres connection string. Defaults to env SUPABASE_DB_URL.",
    )
    parser.add_argument(
        "--via-management-api",
        action="store_true",
        help=(
            "Use the Supabase Management API instead of a direct DB connection. "
            "Requires SUPABASE_PAT and --project-ref (or env SUPABASE_PROJECT_REF)."
        ),
    )
    parser.add_argument(
        "--project-ref",
        default=os.environ.get("SUPABASE_PROJECT_REF"),
        help=(
            "Supabase project ref (the <ref> in <ref>.supabase.co). "
            "Required with --via-management-api. Defaults to env SUPABASE_PROJECT_REF."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not write to the DB. In direct mode this runs the UPSERTs in a "
            "transaction that is rolled back (catches constraint errors). In "
            "Management API mode it only classifies each row as insert/update."
        ),
    )
    args = parser.parse_args()

    with open(args.csv, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        records = [rec for rec in (row_to_record(r) for r in reader) if rec is not None]

    if not records:
        print("error: no usable rows found in CSV.", file=sys.stderr)
        return 1

    if args.via_management_api:
        pat = os.environ.get("SUPABASE_PAT")
        if not pat:
            print("error: SUPABASE_PAT env var is required with --via-management-api.", file=sys.stderr)
            return 2
        if not args.project_ref:
            print(
                "error: --project-ref (or env SUPABASE_PROJECT_REF) is required "
                "with --via-management-api.",
                file=sys.stderr,
            )
            return 2
        return run_via_management_api(records, args.project_ref, pat, args.dry_run)

    if not args.db_url:
        print(
            "error: SUPABASE_DB_URL is not set and --db-url was not provided. "
            "Pass --via-management-api with SUPABASE_PAT to use a personal access "
            "token instead.",
            file=sys.stderr,
        )
        return 2
    return run_via_db_url(records, args.db_url, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
