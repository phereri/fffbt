#!/usr/bin/env python3
"""VPS runtime CLI for the fffbt automation system.

Usage:
    python -m scheduler.cli <command> [options]

Commands:
    discover-devices   Reconcile physical_devices with live ADB / heartbeat state
    reconnect-devices  Reconnect offline devices via adb connect
    sync-drive         Ingest new videos from Google Drive
    create-job         Create one publishing job (reserve video + account + device)
    run-launcher       Start the async job launcher loop
    run-job            Run a single job through the worker pipeline
    status             Show current jobs, devices, and videos summary

Each command accepts --help for detailed usage.
Connection: --db-url / SUPABASE_DB_URL, or --via-management-api with
SUPABASE_PAT and --project-ref / SUPABASE_PROJECT_REF.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
)


@dataclass
class _ConnConfig:
    mode: str  # "direct" or "api"
    db_url: str | None = None
    project_ref: str | None = None
    pat: str | None = None


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
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
        help="Supabase project ref. Required with --via-management-api.",
    )


def _resolve_connection(args: argparse.Namespace) -> _ConnConfig:
    if getattr(args, "via_management_api", False):
        pat = os.environ.get("SUPABASE_PAT")
        if not pat:
            print(
                "error: SUPABASE_PAT env var is required with --via-management-api.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        project_ref = getattr(args, "project_ref", None)
        if not project_ref:
            print(
                "error: --project-ref (or env SUPABASE_PROJECT_REF) is required "
                "with --via-management-api.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return _ConnConfig(mode="api", project_ref=project_ref, pat=pat)

    db_url = getattr(args, "db_url", None) or os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print(
            "error: SUPABASE_DB_URL is not set and --db-url was not provided. "
            "Pass --via-management-api with SUPABASE_PAT to use a personal "
            "access token instead.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return _ConnConfig(mode="direct", db_url=db_url)


def _require_db_url(config: _ConnConfig) -> str:
    if config.mode != "direct" or not config.db_url:
        print(
            "error: this command requires a direct Postgres connection (--db-url). "
            "The Management API does not support persistent connections needed "
            "by the job pipeline.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return config.db_url


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
            "User-Agent": "fffbt-cli/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Management API query failed ({e.code}): {detail}"
        ) from None
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected Management API response: {data!r}")
    return data


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _validate_uuid(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        print(f"error: {label} must be a UUID.", file=sys.stderr)
        raise SystemExit(2)


def _targeted_create_job_sql(device_serial: str, account_id: str | None = None) -> str:
    """SQL for one validation job pinned to a specific online device."""
    serial_sql = _sql_literal(device_serial)
    account_sql = f"{_sql_literal(account_id)}::uuid" if account_id else "NULL::uuid"
    return f"""
WITH requested AS (
    SELECT {serial_sql}::text AS device_serial, {account_sql} AS account_id
),
target_device AS (
    SELECT pd.id
    FROM automation.physical_devices pd, requested r
    WHERE pd.status = 'online'
      AND pd.current_job_id IS NULL
      AND pd.last_seen_at IS NOT NULL
      AND pd.last_seen_at >= now() - interval '300 seconds'
      AND (
          pd.adb_serial = r.device_serial
          OR pd.adb_connect_target = r.device_serial
          OR pd.tailscale_ipv4 = split_part(r.device_serial, ':', 1)
      )
    ORDER BY pd.last_seen_at DESC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
),
eligible_account AS (
    SELECT a.id AS account_id, ae.id AS environment_id
    FROM automation.accounts a
    JOIN automation.account_environments ae ON ae.account_id = a.id
    JOIN automation.proxies p ON p.id = ae.proxy_id AND p.status = 'active'
    JOIN automation.device_profiles dp ON dp.id = ae.device_profile_id AND dp.status = 'active'
    JOIN automation.gps_locations gl ON gl.id = ae.gps_location_id AND gl.status = 'active'
    JOIN automation.app_states aps ON aps.id = ae.app_state_id AND aps.status = 'active'
    CROSS JOIN requested r
    WHERE a.status = 'active'
      AND (r.account_id IS NULL OR a.id = r.account_id)
      AND NOT EXISTS (
          SELECT 1 FROM automation.jobs j
          WHERE j.account_id = a.id
            AND j.status NOT IN ('done', 'failed', 'cancelled')
      )
    ORDER BY a.updated_at ASC NULLS FIRST
    LIMIT 1
),
candidate_video AS (
    SELECT v.id
    FROM automation.videos v
    WHERE v.status = 'new'
      AND EXISTS (SELECT 1 FROM target_device)
      AND EXISTS (SELECT 1 FROM eligible_account)
    ORDER BY v.created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
),
reserved_video AS (
    UPDATE automation.videos v
       SET status = 'reserved'
      FROM candidate_video cv
     WHERE v.id = cv.id
     RETURNING v.*
),
inserted_job AS (
    INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status)
    SELECT rv.id, ea.account_id, ea.environment_id, td.id, 'queued'
      FROM reserved_video rv
      CROSS JOIN eligible_account ea
      CROSS JOIN target_device td
    RETURNING *
),
device_update AS (
    UPDATE automation.physical_devices pd
       SET status = 'busy',
           current_job_id = ij.id
      FROM inserted_job ij
     WHERE pd.id = ij.device_id
    RETURNING pd.id, ij.id AS job_id
),
job_event AS (
    INSERT INTO automation.job_events (job_id, event_type, to_status, payload)
    SELECT ij.id, 'created', 'queued', jsonb_build_object(
        'video_id', ij.video_id,
        'account_id', ij.account_id,
        'environment_id', ij.environment_id,
        'device_id', ij.device_id,
        'target_device_serial', (SELECT device_serial FROM requested),
        'validation_targeted', true
    )
    FROM inserted_job ij
    RETURNING id
),
device_event AS (
    INSERT INTO automation.device_events (device_id, event_type, payload)
    SELECT du.id, 'job_assigned', jsonb_build_object(
        'job_id', du.job_id,
        'target_device_serial', (SELECT device_serial FROM requested),
        'validation_targeted', true
    )
    FROM device_update du
    RETURNING id
)
SELECT * FROM inserted_job;
"""


# ---------------------------------------------------------------------------
# discover-devices / reconnect-devices / sync-drive
# ---------------------------------------------------------------------------


def _forward_to_script(script_module: str, prog: str, argv: list[str]) -> int:
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    saved = sys.argv
    try:
        sys.argv = [prog] + argv
        mod = __import__(script_module)
        return mod.main()
    finally:
        sys.argv = saved


def cmd_discover_devices(argv: list[str]) -> int:
    return _forward_to_script(
        "discover_physical_devices", "fffbt discover-devices", argv
    )


def cmd_reconnect_devices(argv: list[str]) -> int:
    return _forward_to_script(
        "reconnect_devices", "fffbt reconnect-devices", argv
    )


def cmd_sync_drive(argv: list[str]) -> int:
    return _forward_to_script(
        "sync_drive_videos", "fffbt sync-drive", argv
    )


# ---------------------------------------------------------------------------
# create-job
# ---------------------------------------------------------------------------


def cmd_create_job(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fffbt create-job",
        description=(
            "Create one publishing job by calling "
            "automation.create_publishing_job()."
        ),
    )
    _add_connection_args(parser)
    parser.add_argument(
        "--json", action="store_true", help="Print the full job row as JSON."
    )
    parser.add_argument(
        "--device-serial",
        help=(
            "Validation-only: create one queued job pinned to this device "
            "serial/connect target instead of using the generic device pool."
        ),
    )
    parser.add_argument(
        "--account-id",
        help="Validation-only: with --device-serial, also pin the eligible account UUID.",
    )
    args = parser.parse_args(argv)
    config = _resolve_connection(args)
    account_id = _validate_uuid(args.account_id, "--account-id")
    if args.account_id and not args.device_serial:
        print("error: --account-id requires --device-serial.", file=sys.stderr)
        raise SystemExit(2)
    sql = (
        _targeted_create_job_sql(args.device_serial, account_id)
        if args.device_serial
        else "SELECT * FROM automation.create_publishing_job()"
    )

    if config.mode == "api":
        rows = _management_api_query(
            config.project_ref,
            config.pat,
            sql,
        )
        if not rows or rows[0].get("id") is None:
            print("no job created — no eligible video, account, or device available.")
            return 1
        job = rows[0]
    else:
        import psycopg

        with psycopg.connect(config.db_url, autocommit=True) as conn:
            cur = conn.execute(sql)
            row = cur.fetchone()
            if row is None or row[0] is None:
                print("no job created — no eligible video, account, or device available.")
                return 1
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))

    if args.json:
        print(json.dumps(job, default=str, indent=2))
    else:
        print(
            f"job created: id={job['id']} video={job.get('video_id')} "
            f"account={job.get('account_id')} device={job.get('device_id')}"
        )
    return 0


# ---------------------------------------------------------------------------
# run-launcher
# ---------------------------------------------------------------------------


def cmd_run_launcher(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fffbt run-launcher",
        description="Start the async job launcher loop.",
    )
    _add_connection_args(parser)
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "info")
    )
    args = parser.parse_args(argv)
    config = _resolve_connection(args)
    db_url = _require_db_url(config)

    level = args.log_level.upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)s: %(message)s",
    )
    log = logging.getLogger("scheduler")
    log.warning(
        "Worker steps are STUBS — jobs run through the full pipeline "
        "but steps return OK immediately without performing real device "
        "automation or posting."
    )

    from scheduler.launcher import JobLauncher

    launcher = JobLauncher(db_url)
    asyncio.run(launcher.run())
    return 0


# ---------------------------------------------------------------------------
# run-job
# ---------------------------------------------------------------------------


def cmd_run_job(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fffbt run-job",
        description="Run a single job through the worker pipeline.",
    )
    parser.add_argument("job_id", help="UUID of the job to execute.")
    _add_connection_args(parser)
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "info")
    )
    parser.add_argument(
        "--mode",
        choices=("proof_of_posting",),
        default=os.environ.get("FFFBT_MODE"),
        help="Run mode. proof_of_posting requires real worker steps and will not run stubs.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output a structured status/error payload."
    )
    args = parser.parse_args(argv)
    config = _resolve_connection(args)

    def _emit_error(code: str, message: str, *, rc: int = 2) -> int:
        if args.json:
            print(json.dumps({"ok": False, "code": code, "message": message}, indent=2))
        else:
            print(f"error: {message}", file=sys.stderr)
        return rc

    if config.mode == "api":
        return _emit_error(
            "DIRECT_DB_REQUIRED",
            "run-job requires a direct Postgres connection; Management API "
            "cannot run the multi-step worker pipeline.",
        )
    db_url = _require_db_url(config)

    if args.mode == "proof_of_posting":
        return _emit_error(
            "REAL_WORKER_NOT_WIRED",
            "proof_of_posting is not wired into scheduler.run-job yet; the "
            "current default pipeline uses stub worker steps and would not "
            "perform real device automation safely.",
        )

    level = args.log_level.upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(levelname)s: %(message)s",
    )

    async def _run() -> None:
        import psycopg

        async with await psycopg.AsyncConnection.connect(
            db_url, autocommit=True
        ) as conn:
            cur = await conn.execute(
                "SELECT key, value FROM automation.global_settings"
            )
            settings = {row[0]: row[1] for row in await cur.fetchall()}

        from scheduler.pipeline import run_job_pipeline

        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown.set)

        await run_job_pipeline(
            db_url=db_url,
            job={"id": args.job_id},
            settings=settings,
            shutdown=shutdown,
        )

    asyncio.run(_run())
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

_STATUS_QUERIES = {
    "jobs": "SELECT status, count(*) FROM automation.jobs GROUP BY status ORDER BY status",
    "devices": "SELECT status, count(*) FROM automation.physical_devices GROUP BY status ORDER BY status",
    "videos": "SELECT status, count(*) FROM automation.videos GROUP BY status ORDER BY status",
}


def cmd_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="fffbt status",
        description="Show current jobs, devices, and videos summary.",
    )
    _add_connection_args(parser)
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON."
    )
    parser.add_argument(
        "--events",
        type=int,
        default=0,
        metavar="N",
        help="Show the N most recent job events.",
    )
    args = parser.parse_args(argv)
    config = _resolve_connection(args)

    result: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    if config.mode == "api":
        for section, sql in _STATUS_QUERIES.items():
            rows = _management_api_query(config.project_ref, config.pat, sql)
            result[section] = {r["status"]: r["count"] for r in rows}

        if args.events > 0:
            events_sql = (
                "SELECT id, job_id, event_type, from_status, "
                "to_status, created_at, payload "
                "FROM automation.job_events "
                f"ORDER BY created_at DESC LIMIT {int(args.events)}"
            )
            events = _management_api_query(
                config.project_ref, config.pat, events_sql
            )
    else:
        import psycopg

        with psycopg.connect(config.db_url) as conn:
            with conn.cursor() as cur:
                for section, sql in _STATUS_QUERIES.items():
                    cur.execute(sql)
                    result[section] = {row[0]: row[1] for row in cur.fetchall()}

                if args.events > 0:
                    cur.execute(
                        "SELECT id, job_id, event_type, from_status, "
                        "to_status, created_at, payload "
                        "FROM automation.job_events "
                        "ORDER BY created_at DESC LIMIT %s",
                        (args.events,),
                    )
                    cols = [d.name for d in cur.description]
                    events = [dict(zip(cols, r)) for r in cur.fetchall()]

    if events:
        result["recent_events"] = events

    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        for section in ("jobs", "devices", "videos"):
            print(f"{section.capitalize()}:")
            counts = result[section]
            if counts:
                for status, count in sorted(counts.items()):
                    print(f"  {status}: {count}")
            else:
                print("  (none)")

        if events:
            print(f"\nRecent events ({len(events)}):")
            for e in events:
                ts = str(e.get("created_at", ""))[:19]
                print(
                    f"  [{ts}] {e.get('event_type', '')} "
                    f"job={str(e.get('job_id', ''))[:8]} "
                    f"{e.get('from_status', '') or ''}"
                    f"{' -> ' if e.get('to_status') else ''}"
                    f"{e.get('to_status', '') or ''}"
                )
    return 0


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

COMMANDS = {
    "discover-devices": cmd_discover_devices,
    "reconnect-devices": cmd_reconnect_devices,
    "sync-drive": cmd_sync_drive,
    "create-job": cmd_create_job,
    "run-launcher": cmd_run_launcher,
    "run-job": cmd_run_job,
    "status": cmd_status,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(__doc__.strip())
        return 0 if sys.argv[1:] == ["--help"] or sys.argv[1:] == ["-h"] else 2

    command = sys.argv[1]
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"error: unknown command '{command}'", file=sys.stderr)
        print(f"available: {', '.join(COMMANDS)}", file=sys.stderr)
        return 2

    return handler(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
