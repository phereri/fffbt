#!/usr/bin/env python3
"""Reconcile automation.physical_devices with live device sources.

Reads from one or both sources (never writes to them, never runs destructive ADB
commands) and updates automation.physical_devices:

  * --source adb        runs `adb devices -l` locally
  * --source heartbeat  reads recent rows from public.device_heartbeats
                        (legacy heartbeat writer feeds this; we only SELECT)
  * --source both       union of both (default)

Discovery is non-destructive:

  * Inserts a automation.device_events row when status changes ('connected' /
    'disconnected'). No state-changing ADB commands are issued — `adb connect`
    is FFF-47's responsibility, not ours.
  * Never overwrites a non-null adb_serial unless --reassign-serial.
  * Never modifies current_job_id or transitions 'busy' / 'maintenance' rows.

Usage:

    SUPABASE_DB_URL=postgresql://... \\
    python scripts/discover_physical_devices.py [--source both] [--dry-run]

A device row is matched to a live serial by:

  1. ADB serial of the form '<ip>:<port>' → physical_devices.tailscale_ipv4
  2. Plain USB serial → physical_devices.adb_serial (once known)

A row that hasn't been seen by either source within --stale-seconds (default
120, matching automation.global_settings.job_heartbeat_timeout_seconds) is
flipped to 'offline'.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_STALE_SECONDS = 120

# ADB TCP serials look like "100.68.78.96:5555". Anything with a colon and a
# valid IPv4 on the left is treated as a TCP serial.
TCP_SERIAL_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d+$")

# `adb devices -l` row, e.g.:
#   100.68.78.96:5555      device product:o1q model:SM_N950F device:o1q transport_id:1
#   abcd1234               offline
ADB_ROW_RE = re.compile(r"^(\S+)\s+(\S+)(?:\s+(.*))?$")


@dataclass
class LiveDevice:
    serial: str            # ADB serial as reported by the source
    state: str             # 'device', 'offline', 'unauthorized', etc.
    ip: str | None         # IPv4 if serial is TCP-form, else None
    seen_at: datetime      # source-reported timestamp
    source: str            # 'adb' | 'heartbeat'


# --- Source: local ADB --------------------------------------------------------


def parse_adb_devices_l(stdout: str, now: datetime) -> list[LiveDevice]:
    """Parse the body of `adb devices -l`. Header line is skipped."""
    out: list[LiveDevice] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        m = ADB_ROW_RE.match(line)
        if not m:
            continue
        serial, state, _attrs = m.groups()
        ip = None
        tcp = TCP_SERIAL_RE.match(serial)
        if tcp:
            ip = tcp.group(1)
        out.append(LiveDevice(
            serial=serial, state=state, ip=ip, seen_at=now, source="adb"
        ))
    return out


def adb_devices(adb_bin: str | None) -> list[LiveDevice]:
    bin_path = adb_bin or shutil.which("adb")
    if not bin_path:
        raise RuntimeError(
            "adb binary not found in PATH; pass --adb-bin or remove 'adb' from --source"
        )
    proc = subprocess.run(
        [bin_path, "devices", "-l"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return parse_adb_devices_l(proc.stdout, datetime.now(timezone.utc))


# --- Source: heartbeat table --------------------------------------------------


HEARTBEAT_SQL = """
    SELECT serial, state, ip, seen_at
    FROM public.device_heartbeats
    WHERE seen_at > now() - make_interval(secs => %(stale)s)
"""


def fetch_heartbeats(conn, stale_seconds: int) -> list[LiveDevice]:
    with conn.cursor() as cur:
        cur.execute(HEARTBEAT_SQL, {"stale": stale_seconds})
        rows = cur.fetchall()
    out: list[LiveDevice] = []
    for serial, state, ip, seen_at in rows:
        # If the heartbeat row didn't capture an IP but the serial is TCP-form,
        # derive the IP from the serial.
        derived_ip = ip
        if not derived_ip:
            m = TCP_SERIAL_RE.match(serial or "")
            if m:
                derived_ip = m.group(1)
        out.append(LiveDevice(
            serial=serial, state=state, ip=derived_ip,
            seen_at=seen_at, source="heartbeat",
        ))
    return out


# --- Reconciliation -----------------------------------------------------------


@dataclass
class DeviceRow:
    id: str
    alias: str
    adb_serial: str | None
    tailscale_ipv4: str | None
    status: str
    last_seen_at: datetime | None


def fetch_physical_devices(conn) -> list[DeviceRow]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, alias, adb_serial, tailscale_ipv4, status, last_seen_at "
            "FROM automation.physical_devices"
        )
        return [DeviceRow(*r) for r in cur.fetchall()]


def index_live(live: list[LiveDevice]) -> tuple[dict[str, LiveDevice], dict[str, LiveDevice]]:
    """Return (by_serial, by_ip), keeping the most recent observation per key."""
    by_serial: dict[str, LiveDevice] = {}
    by_ip: dict[str, LiveDevice] = {}
    for d in live:
        prior = by_serial.get(d.serial)
        if prior is None or d.seen_at > prior.seen_at:
            by_serial[d.serial] = d
        if d.ip:
            prior_ip = by_ip.get(d.ip)
            if prior_ip is None or d.seen_at > prior_ip.seen_at:
                by_ip[d.ip] = d
    return by_serial, by_ip


def is_online_state(state: str) -> bool:
    """An ADB device is reachable only when state == 'device'."""
    return state == "device"


@dataclass
class Plan:
    row: DeviceRow
    matched: LiveDevice | None
    new_status: str
    new_last_seen_at: datetime | None
    new_adb_serial: str | None
    event: str | None  # 'connected' / 'disconnected' / None


def build_plan(
    rows: list[DeviceRow],
    by_serial: dict[str, LiveDevice],
    by_ip: dict[str, LiveDevice],
    stale_threshold: datetime,
    reassign_serial: bool,
) -> list[Plan]:
    plans: list[Plan] = []
    for row in rows:
        match: LiveDevice | None = None

        if row.adb_serial and row.adb_serial in by_serial:
            match = by_serial[row.adb_serial]
        if match is None and row.tailscale_ipv4 and row.tailscale_ipv4 in by_ip:
            match = by_ip[row.tailscale_ipv4]

        fresh = (
            match is not None
            and match.seen_at >= stale_threshold
            and is_online_state(match.state)
        )

        new_status = row.status
        new_last_seen = row.last_seen_at
        new_serial: str | None = None
        event: str | None = None

        # Don't touch reservations or operator-set states.
        if row.status in ("busy", "maintenance"):
            plans.append(Plan(row, match, new_status, new_last_seen, None, None))
            continue

        if fresh:
            assert match is not None  # mypy
            new_last_seen = (
                match.seen_at if new_last_seen is None or match.seen_at > new_last_seen
                else new_last_seen
            )
            if row.status != "online":
                new_status = "online"
                event = "connected"

            # Backfill adb_serial when we matched only by IP and discovered a
            # USB-form serial in the heartbeat / adb output.
            tcp = TCP_SERIAL_RE.match(match.serial)
            if not tcp and (row.adb_serial is None or (reassign_serial and row.adb_serial != match.serial)):
                if row.adb_serial != match.serial:
                    new_serial = match.serial
        else:
            if row.status == "online":
                new_status = "offline"
                event = "disconnected"

        plans.append(Plan(row, match, new_status, new_last_seen, new_serial, event))
    return plans


# --- Apply --------------------------------------------------------------------


UPDATE_SQL = """
    UPDATE automation.physical_devices
    SET status       = %(status)s,
        last_seen_at = COALESCE(%(last_seen_at)s, last_seen_at),
        adb_serial   = COALESCE(%(adb_serial)s, adb_serial)
    WHERE id = %(id)s
"""

EVENT_SQL = """
    INSERT INTO automation.device_events (device_id, event_type, payload)
    VALUES (%(device_id)s, %(event_type)s, %(payload)s::jsonb)
"""


def apply_plan(conn, plans: list[Plan]) -> dict[str, int]:
    counts = {"online": 0, "offline": 0, "noop": 0, "serial_set": 0}
    with conn.cursor() as cur:
        for p in plans:
            changed_status = p.new_status != p.row.status
            changed_serial = p.new_adb_serial is not None
            changed_seen = (
                p.new_last_seen_at is not None
                and p.new_last_seen_at != p.row.last_seen_at
            )
            if not (changed_status or changed_serial or changed_seen):
                counts["noop"] += 1
                continue

            cur.execute(UPDATE_SQL, {
                "id": p.row.id,
                "status": p.new_status,
                "last_seen_at": p.new_last_seen_at,
                "adb_serial": p.new_adb_serial,
            })
            if p.event == "connected":
                counts["online"] += 1
            elif p.event == "disconnected":
                counts["offline"] += 1
            if changed_serial:
                counts["serial_set"] += 1

            if p.event:
                import json
                payload = {
                    "source": p.matched.source if p.matched else None,
                    "matched_serial": p.matched.serial if p.matched else None,
                    "matched_state": p.matched.state if p.matched else None,
                }
                cur.execute(EVENT_SQL, {
                    "device_id": p.row.id,
                    "event_type": p.event,
                    "payload": json.dumps(payload),
                })
    return counts


def format_plan(p: Plan) -> str:
    bits = [f"id={p.row.id}", f"alias={p.row.alias}"]
    if p.new_status != p.row.status:
        bits.append(f"status: {p.row.status} -> {p.new_status}")
    else:
        bits.append(f"status={p.row.status}")
    if p.new_adb_serial:
        bits.append(f"adb_serial: {p.row.adb_serial!r} -> {p.new_adb_serial!r}")
    if p.matched:
        bits.append(f"src={p.matched.source}({p.matched.state})")
    return " ".join(bits)


# --- Main ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile live ADB / heartbeat state with automation.physical_devices.",
    )
    parser.add_argument(
        "--source",
        choices=("adb", "heartbeat", "both"),
        default="both",
        help="Where to pull live device state from (default: both).",
    )
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=DEFAULT_STALE_SECONDS,
        help=f"Treat heartbeats older than this as not-seen (default: {DEFAULT_STALE_SECONDS}).",
    )
    parser.add_argument(
        "--adb-bin",
        default=os.environ.get("ADB_BIN"),
        help="Path to the adb binary. Defaults to env ADB_BIN, then $PATH.",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("SUPABASE_DB_URL"),
        help="Postgres connection string. Defaults to env SUPABASE_DB_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and roll back. No rows are modified.",
    )
    parser.add_argument(
        "--reassign-serial",
        action="store_true",
        help=(
            "Allow overwriting a non-null adb_serial when discovery reports a "
            "different serial for the same device. Off by default."
        ),
    )
    args = parser.parse_args()

    if not args.db_url:
        print("error: SUPABASE_DB_URL is not set and --db-url was not provided.",
              file=sys.stderr)
        return 2

    import psycopg

    conn = psycopg.connect(args.db_url)
    try:
        live: list[LiveDevice] = []
        if args.source in ("adb", "both"):
            live.extend(adb_devices(args.adb_bin))
        if args.source in ("heartbeat", "both"):
            live.extend(fetch_heartbeats(conn, args.stale_seconds))

        rows = fetch_physical_devices(conn)
        by_serial, by_ip = index_live(live)
        stale_threshold = datetime.now(timezone.utc) - timedelta(seconds=args.stale_seconds)
        plans = build_plan(
            rows, by_serial, by_ip, stale_threshold, args.reassign_serial
        )

        for p in plans:
            if (
                p.new_status != p.row.status
                or p.new_adb_serial is not None
                or p.event is not None
            ):
                print(format_plan(p))

        counts = apply_plan(conn, plans)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

        prefix = "DRY RUN: " if args.dry_run else ""
        print(
            f"{prefix}live={len(live)} rows={len(rows)} "
            f"online+{counts['online']} offline+{counts['offline']} "
            f"serial_set={counts['serial_set']} noop={counts['noop']}"
        )
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
