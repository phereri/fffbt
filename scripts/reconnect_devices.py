#!/usr/bin/env python3
"""Reconnect offline Android devices via ADB TCP.

Runs ``adb connect <tailscale_ipv4>:5555`` for one or all offline devices and
flips successful reconnects from ``offline`` to ``online`` in
``automation.physical_devices``.

This is a safe ops script:

  * ``busy`` and ``maintenance`` rows are never touched (skipped with a
    warning when explicitly selected).
  * ``online`` rows are skipped (already connected).
  * No destructive ADB commands are issued — ``adb connect`` only establishes
    a TCP socket; it does not modify the device.
  * Dry-run prints the planned actions and exits without touching adb or DB.

Two transports for the DB, mirroring ``import_physical_devices.py`` and
``discover_physical_devices.py``:

  1. Direct Postgres (default). Needs ``SUPABASE_DB_URL`` or ``--db-url``.

         SUPABASE_DB_URL=postgresql://... \\
         python scripts/reconnect_devices.py --all [--dry-run]

  2. Supabase Management API. Needs ``SUPABASE_PAT`` and ``--project-ref``
     (or env ``SUPABASE_PROJECT_REF``).

         SUPABASE_PAT=sbp_... \\
         python scripts/reconnect_devices.py \\
             --via-management-api --project-ref <ref> --all [--dry-run]

Selectors:

  --all              Reconnect every offline device with a known IPv4.
  --device <value>   Reconnect a single device matched by alias, device_id,
                     tailscale_ipv4, or row UUID. Exactly one of --all or
                     --device must be set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_ADB_PORT = 5555
DEFAULT_ADB_TIMEOUT = 15  # seconds; caps a single `adb connect` invocation


# `adb connect` output patterns we care about. adb prints to stdout on
# success and on most failures, but Cloudflare-flavoured network errors
# sometimes land on stderr — combine both before classifying.
_ALREADY_RE = re.compile(r"already connected to", re.IGNORECASE)
_CONNECTED_RE = re.compile(r"(^|\n)\s*connected to", re.IGNORECASE)
_FAILED_RE = re.compile(
    r"(failed to connect|cannot connect|unable to connect|"
    r"connection refused|no route to host|timeout|timed out)",
    re.IGNORECASE,
)


@dataclass
class DeviceRow:
    id: str
    alias: str
    device_id: str | None
    adb_serial: str | None
    tailscale_ipv4: str | None
    adb_connect_target: str | None
    status: str


@dataclass
class ConnectResult:
    row: DeviceRow
    target: str          # ip:port we ran adb connect against, or '' if skipped
    outcome: str         # 'connected' | 'already' | 'failed' | 'skipped' | 'dry-run'
    message: str         # combined stdout/stderr (or skip reason)
    exit_code: int       # adb exit code, -1 if not run


# --- Classification ----------------------------------------------------------


def classify_adb_output(stdout: str, stderr: str, exit_code: int) -> str:
    """Classify the result of an ``adb connect`` invocation.

    adb's exit code is 0 even when it prints ``failed to connect``, so the
    text is authoritative. Treat anything we don't recognise as a success
    as a failure.
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    if _ALREADY_RE.search(combined):
        return "already"
    if _CONNECTED_RE.search(combined):
        return "connected"
    if _FAILED_RE.search(combined):
        return "failed"
    if exit_code != 0:
        return "failed"
    return "failed"


# --- Device selection --------------------------------------------------------


def filter_reconnect_candidates(rows: list[DeviceRow]) -> tuple[list[DeviceRow], list[ConnectResult]]:
    """Return (offline rows with a target, skip results for the rest).

    Used by ``--all``: every offline row with a non-null ``adb_connect_target``
    is a candidate. Rows in other statuses are returned as ``skipped`` results
    so the caller can log them.
    """
    candidates: list[DeviceRow] = []
    skipped: list[ConnectResult] = []
    for row in rows:
        if row.status == "offline":
            if row.adb_connect_target:
                candidates.append(row)
            else:
                skipped.append(ConnectResult(
                    row=row, target="", outcome="skipped",
                    message="no adb_connect_target set", exit_code=-1,
                ))
        elif row.status == "online":
            skipped.append(ConnectResult(
                row=row, target=row.adb_connect_target or "",
                outcome="skipped", message="already online", exit_code=-1,
            ))
        else:
            skipped.append(ConnectResult(
                row=row, target=row.adb_connect_target or "",
                outcome="skipped",
                message=f"status={row.status}; protected from reconnect",
                exit_code=-1,
            ))
    return candidates, skipped


def select_single_device(rows: list[DeviceRow], selector: str) -> DeviceRow:
    """Resolve ``--device <value>`` to exactly one row.

    Match priority: id (UUID) > alias > device_id > tailscale_ipv4. A value
    that matches different rows in different fields is ambiguous; raise.
    """
    selector = selector.strip()
    if not selector:
        raise ValueError("--device value cannot be empty")

    matches: dict[str, DeviceRow] = {}
    for row in rows:
        for field_value in (row.id, row.alias, row.device_id, row.tailscale_ipv4):
            if field_value and field_value == selector:
                matches[row.id] = row
                break

    if not matches:
        raise LookupError(f"no physical_devices row matches {selector!r}")
    if len(matches) > 1:
        aliases = ", ".join(sorted(r.alias for r in matches.values()))
        raise LookupError(
            f"selector {selector!r} matches multiple rows: {aliases}"
        )
    return next(iter(matches.values()))


# --- ADB invocation ----------------------------------------------------------


def adb_connect(adb_bin: str, target: str, timeout: int) -> tuple[int, str, str]:
    """Run ``adb connect <target>``. Returns (exit_code, stdout, stderr).

    A subprocess.TimeoutExpired is converted into a synthetic
    ``timed out after Ns`` failure so the caller's classification still works.
    """
    try:
        proc = subprocess.run(
            [adb_bin, "connect", target],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + f"\ntimed out after {timeout}s"


def reconnect_device(adb_bin: str, row: DeviceRow, timeout: int) -> ConnectResult:
    target = row.adb_connect_target
    if not target:
        return ConnectResult(
            row=row, target="", outcome="skipped",
            message="no adb_connect_target set", exit_code=-1,
        )
    code, stdout, stderr = adb_connect(adb_bin, target, timeout)
    outcome = classify_adb_output(stdout, stderr, code)
    return ConnectResult(
        row=row,
        target=target,
        outcome=outcome,
        message=((stdout or "") + (("\n" + stderr) if stderr else "")).strip(),
        exit_code=code,
    )


# --- DB transports -----------------------------------------------------------


# Selecting only what the script needs keeps the row narrow.
DEVICES_COLS = "id::text, alias, device_id, adb_serial, tailscale_ipv4, adb_connect_target, status"


def fetch_devices_db(conn) -> list[DeviceRow]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {DEVICES_COLS} FROM automation.physical_devices")
        return [DeviceRow(*r) for r in cur.fetchall()]


UPDATE_SQL = """
    UPDATE automation.physical_devices
    SET status       = 'online',
        last_seen_at = now()
    WHERE id = %(id)s
      AND status = 'offline'
"""

EVENT_SUCCESS_SQL = """
    INSERT INTO automation.device_events (device_id, event_type, payload)
    VALUES (%(id)s, 'connected', %(payload)s::jsonb)
"""

EVENT_ERROR_SQL = """
    INSERT INTO automation.device_events (device_id, event_type, payload)
    VALUES (%(id)s, 'error', %(payload)s::jsonb)
"""


def apply_results_db(conn, results: list[ConnectResult]) -> None:
    with conn.cursor() as cur:
        for r in results:
            payload = json.dumps({
                "source": "reconnect",
                "target": r.target,
                "outcome": r.outcome,
                "exit_code": r.exit_code,
                "message": r.message[:1000],  # cap to keep payload small
            })
            if r.outcome in ("connected", "already"):
                cur.execute(UPDATE_SQL, {"id": r.row.id})
                cur.execute(EVENT_SUCCESS_SQL, {"id": r.row.id, "payload": payload})
            elif r.outcome == "failed":
                cur.execute(EVENT_ERROR_SQL, {"id": r.row.id, "payload": payload})


# --- Supabase Management API -------------------------------------------------


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
            # Cloudflare in front of api.supabase.com rejects the default
            # Python-urllib User-Agent with a 403 / error 1010.
            "User-Agent": "fffbt-reconnect-devices/1.0",
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


def fetch_devices_api(project_ref: str, pat: str) -> list[DeviceRow]:
    sql = f"SELECT {DEVICES_COLS} FROM automation.physical_devices"
    rows = _management_api_query(project_ref, pat, sql)
    return [
        DeviceRow(
            id=r["id"],
            alias=r["alias"],
            device_id=r.get("device_id"),
            adb_serial=r.get("adb_serial"),
            tailscale_ipv4=r.get("tailscale_ipv4"),
            adb_connect_target=r.get("adb_connect_target"),
            status=r["status"],
        )
        for r in rows
    ]


def apply_results_api(project_ref: str, pat: str, results: list[ConnectResult]) -> None:
    """Apply all DB updates and event inserts in one CTE call."""
    changes: list[dict] = []
    for r in results:
        if r.outcome in ("connected", "already", "failed"):
            changes.append({
                "id": r.row.id,
                "outcome": r.outcome,
                "target": r.target,
                "exit_code": r.exit_code,
                "message": r.message[:1000],
            })
    if not changes:
        return

    payload = json.dumps(changes).replace("'", "''")
    sql = f"""
        WITH input AS (
          SELECT * FROM jsonb_to_recordset('{payload}'::jsonb)
            AS t(
              id uuid, outcome text, target text,
              exit_code int, message text
            )
        ),
        updated AS (
          UPDATE automation.physical_devices pd
          SET status       = 'online',
              last_seen_at = now()
          FROM input i
          WHERE pd.id = i.id
            AND i.outcome IN ('connected', 'already')
            AND pd.status = 'offline'
          RETURNING pd.id
        ),
        events AS (
          INSERT INTO automation.device_events (device_id, event_type, payload)
          SELECT i.id,
                 CASE WHEN i.outcome IN ('connected', 'already')
                      THEN 'connected' ELSE 'error' END,
                 jsonb_build_object(
                   'source', 'reconnect',
                   'target', i.target,
                   'outcome', i.outcome,
                   'exit_code', i.exit_code,
                   'message', i.message
                 )
          FROM input i
          RETURNING device_id
        )
        SELECT
          (SELECT count(*) FROM updated) AS updated_count,
          (SELECT count(*) FROM events) AS event_count;
    """
    _management_api_query(project_ref, pat, sql)


# --- Output ------------------------------------------------------------------


def format_result(r: ConnectResult) -> str:
    target = r.target or "-"
    head = f"alias={r.row.alias} target={target}"
    if r.outcome in ("connected", "already"):
        return f"  OK    {head} → {r.outcome}"
    if r.outcome == "skipped":
        return f"  SKIP  {head} ({r.message})"
    if r.outcome == "dry-run":
        return f"  PLAN  {head} (would run `adb connect {target}`)"
    return f"  FAIL  {head} → {r.outcome}: {r.message.splitlines()[0] if r.message else ''}"


def summarise(results: list[ConnectResult], dry_run: bool) -> dict[str, int]:
    counts = {"connected": 0, "already": 0, "failed": 0, "skipped": 0, "dry-run": 0}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    prefix = "DRY RUN: " if dry_run else ""
    print(
        f"{prefix}connected+{counts['connected']} already={counts['already']} "
        f"failed={counts['failed']} skipped={counts['skipped']} "
        f"planned={counts['dry-run']}"
    )
    return counts


# --- Main --------------------------------------------------------------------


def _resolve_adb_bin(adb_bin: str | None) -> str:
    bin_path = adb_bin or shutil.which("adb")
    if not bin_path:
        raise RuntimeError(
            "adb binary not found in PATH; pass --adb-bin or set ADB_BIN"
        )
    return bin_path


def _pick_targets(
    rows: list[DeviceRow], all_flag: bool, device: str | None
) -> tuple[list[DeviceRow], list[ConnectResult]]:
    if all_flag:
        return filter_reconnect_candidates(rows)
    assert device is not None
    row = select_single_device(rows, device)
    if row.status == "offline":
        if not row.adb_connect_target:
            return [], [ConnectResult(
                row=row, target="", outcome="skipped",
                message="no adb_connect_target set", exit_code=-1,
            )]
        return [row], []
    return [], [ConnectResult(
        row=row, target=row.adb_connect_target or "",
        outcome="skipped",
        message=(
            "already online" if row.status == "online"
            else f"status={row.status}; protected from reconnect"
        ),
        exit_code=-1,
    )]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconnect offline Android devices via `adb connect <ip>:5555`."
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--all", action="store_true",
        help="Reconnect every offline device with a known adb_connect_target.",
    )
    selector.add_argument(
        "--device",
        help=(
            "Reconnect a single device. Matched by id (UUID), alias, device_id, "
            "or tailscale_ipv4."
        ),
    )
    parser.add_argument(
        "--adb-bin",
        default=os.environ.get("ADB_BIN"),
        help="Path to the adb binary. Defaults to env ADB_BIN, then $PATH.",
    )
    parser.add_argument(
        "--adb-timeout",
        type=int,
        default=DEFAULT_ADB_TIMEOUT,
        help=f"Per-device adb connect timeout in seconds (default: {DEFAULT_ADB_TIMEOUT}).",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("SUPABASE_DB_URL"),
        help="Postgres connection string. Defaults to env SUPABASE_DB_URL.",
    )
    parser.add_argument(
        "--via-management-api", action="store_true",
        help=(
            "Use the Supabase Management API instead of a direct DB connection. "
            "Requires SUPABASE_PAT and --project-ref (or env SUPABASE_PROJECT_REF)."
        ),
    )
    parser.add_argument(
        "--project-ref",
        default=os.environ.get("SUPABASE_PROJECT_REF"),
        help="Supabase project ref. Required with --via-management-api.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the planned actions and exit without running adb or touching the DB.",
    )
    args = parser.parse_args()

    if args.via_management_api:
        return _run_via_management_api(args)
    return _run_via_db_url(args)


def _run_with_rows(
    rows: list[DeviceRow], args, apply_fn
) -> int:
    targets, skipped = _pick_targets(rows, args.all, args.device)

    for r in skipped:
        print(format_result(r))

    if args.dry_run:
        planned = [
            ConnectResult(
                row=row, target=row.adb_connect_target or "",
                outcome="dry-run",
                message="planned",
                exit_code=-1,
            )
            for row in targets
        ]
        for r in planned:
            print(format_result(r))
        summarise(skipped + planned, dry_run=True)
        return 0

    adb_bin = _resolve_adb_bin(args.adb_bin)
    results: list[ConnectResult] = list(skipped)
    for row in targets:
        res = reconnect_device(adb_bin, row, args.adb_timeout)
        print(format_result(res), flush=True)
        results.append(res)

    apply_fn(results)
    summarise(results, dry_run=False)
    # Exit non-zero when any attempted reconnect failed so cron / CI can notice.
    return 1 if any(r.outcome == "failed" for r in results) else 0


def _run_via_db_url(args) -> int:
    if not args.db_url:
        print(
            "error: SUPABASE_DB_URL is not set and --db-url was not provided. "
            "Pass --via-management-api with SUPABASE_PAT to use a personal "
            "access token instead.",
            file=sys.stderr,
        )
        return 2

    import psycopg

    conn = psycopg.connect(args.db_url)
    try:
        rows = fetch_devices_db(conn)
        return _run_with_rows(
            rows, args,
            apply_fn=lambda results: (apply_results_db(conn, results), conn.commit()),
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_via_management_api(args) -> int:
    pat = os.environ.get("SUPABASE_PAT")
    if not pat:
        print(
            "error: SUPABASE_PAT env var is required with --via-management-api.",
            file=sys.stderr,
        )
        return 2
    if not args.project_ref:
        print(
            "error: --project-ref (or env SUPABASE_PROJECT_REF) is required "
            "with --via-management-api.",
            file=sys.stderr,
        )
        return 2

    rows = fetch_devices_api(args.project_ref, pat)
    return _run_with_rows(
        rows, args,
        apply_fn=lambda results: apply_results_api(args.project_ref, pat, results),
    )


if __name__ == "__main__":
    raise SystemExit(main())
