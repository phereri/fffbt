#!/usr/bin/env python3
"""Import a Tailscale device CSV export into automation.physical_devices.

Usage:
    python scripts/import_physical_devices.py --csv <path> [--dry-run] [--db-url <url>]

`SUPABASE_DB_URL` is read from the environment when `--db-url` is omitted.

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
import os
import sys
from datetime import datetime, timezone

import psycopg

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


def upsert(conn: psycopg.Connection, record: dict) -> str:
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
        "--dry-run",
        action="store_true",
        help="Parse the CSV and apply the changes inside a transaction that is rolled back.",
    )
    args = parser.parse_args()

    if not args.db_url:
        print("error: SUPABASE_DB_URL is not set and --db-url was not provided.", file=sys.stderr)
        return 2

    with open(args.csv, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        records = [rec for rec in (row_to_record(r) for r in reader) if rec is not None]

    if not records:
        print("error: no usable rows found in CSV.", file=sys.stderr)
        return 1

    counts = {"insert": 0, "update": 0}
    conn = psycopg.connect(args.db_url)
    try:
        for rec in records:
            action = upsert(conn, rec)
            counts[action] += 1
            print(
                f"{action}: device_id={rec['device_id']} alias={rec['alias']} "
                f"ipv4={rec['tailscale_ipv4']} last_seen_at={rec['last_seen_at']}"
            )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    prefix = "DRY RUN: " if args.dry_run else ""
    print(
        f"{prefix}{counts['insert']} insert(s), {counts['update']} update(s), "
        f"{len(records)} total."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
