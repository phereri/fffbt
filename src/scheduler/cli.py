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
Environment: SUPABASE_DB_URL must be set for database commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any

SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
)


def _require_db_url(args: argparse.Namespace) -> str:
    db_url = getattr(args, "db_url", None) or os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print(
            "error: SUPABASE_DB_URL is not set and --db-url was not provided.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return db_url


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
    parser.add_argument("--db-url", default=os.environ.get("SUPABASE_DB_URL"))
    parser.add_argument(
        "--json", action="store_true", help="Print the full job row as JSON."
    )
    args = parser.parse_args(argv)
    db_url = _require_db_url(args)

    import psycopg

    with psycopg.connect(db_url, autocommit=True) as conn:
        cur = conn.execute("SELECT * FROM automation.create_publishing_job()")
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
    parser.add_argument("--db-url", default=os.environ.get("SUPABASE_DB_URL"))
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "info")
    )
    args = parser.parse_args(argv)
    db_url = _require_db_url(args)

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
    parser.add_argument("--db-url", default=os.environ.get("SUPABASE_DB_URL"))
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "info")
    )
    args = parser.parse_args(argv)
    db_url = _require_db_url(args)

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
    parser.add_argument("--db-url", default=os.environ.get("SUPABASE_DB_URL"))
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
    db_url = _require_db_url(args)

    import psycopg

    result: dict[str, Any] = {}
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for section, sql in _STATUS_QUERIES.items():
                cur.execute(sql)
                result[section] = {row[0]: row[1] for row in cur.fetchall()}

            events: list[dict[str, Any]] = []
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
