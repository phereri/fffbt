"""Async job launcher for the fffbt scheduler.

Creates publishing jobs via automation.create_publishing_job() and dispatches
each to an async worker task, bounded by asyncio.Semaphore. The scheduler
never executes jobs itself -- it reserves resources and hands off to workers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg

log = logging.getLogger("scheduler")


def _jsonl(event: str, **kw: Any) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    sys.stderr.write(json.dumps(record, default=str) + "\n")
    sys.stderr.flush()


INFRA_ERRORS = (ConnectionError, TimeoutError, OSError, psycopg.OperationalError)
MAX_INFRA_RETRIES = 3
INFRA_RETRY_BACKOFF = 2.0


async def _load_settings(conn: psycopg.AsyncConnection) -> dict[str, str]:
    cur = await conn.execute("SELECT key, value FROM automation.global_settings")
    return {row[0]: row[1] for row in await cur.fetchall()}


async def _create_publishing_job(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM automation.create_publishing_job()")
    row = await cur.fetchone()
    if row is None:
        return None
    cols = [desc.name for desc in cur.description]
    result = dict(zip(cols, row))
    if result.get("id") is None:
        return None
    return result


async def _count_active_jobs(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM automation.jobs "
        "WHERE status NOT IN ('done', 'failed', 'cancelled', 'needs_review')"
    )
    row = await cur.fetchone()
    return row[0]


async def _transition_job(
    conn: psycopg.AsyncConnection,
    job_id: str,
    to_status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    payload = {}
    if error_code:
        payload["error_code"] = error_code
    if error_message:
        payload["error_message"] = error_message
    await conn.execute(
        "SELECT automation.transition_job_status(%s, %s, %s::jsonb)",
        (job_id, to_status, json.dumps(payload) if payload else None),
    )


async def _update_heartbeat(conn: psycopg.AsyncConnection, job_id: str) -> None:
    await conn.execute(
        "UPDATE automation.jobs SET updated_at = now() WHERE id = %s", (job_id,)
    )


async def _detect_stale_jobs(
    conn: psycopg.AsyncConnection, timeout_seconds: int
) -> list[str]:
    cur = await conn.execute(
        "SELECT id FROM automation.jobs "
        "WHERE status NOT IN ('done', 'failed', 'cancelled', 'needs_review', 'queued') "
        "AND updated_at < now() - make_interval(secs => %s)",
        (timeout_seconds,),
    )
    return [row[0] for row in await cur.fetchall()]


async def _release_device(conn: psycopg.AsyncConnection, job_id: str) -> None:
    cur = await conn.execute(
        "UPDATE automation.physical_devices "
        "SET status = 'online', current_job_id = NULL "
        "WHERE current_job_id = %s RETURNING id",
        (job_id,),
    )
    row = await cur.fetchone()
    if row:
        await conn.execute(
            "INSERT INTO automation.device_events (device_id, event_type, payload) "
            "VALUES (%s, 'job_released', %s::jsonb)",
            (row[0], json.dumps({"job_id": job_id})),
        )


class JobLauncher:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self._semaphore: asyncio.Semaphore | None = None
        self._active: dict[str, asyncio.Task] = {}
        self._shutdown = asyncio.Event()
        self._settings: dict[str, str] = {}
        self.stats = {
            "created": 0,
            "done": 0,
            "failed": 0,
            "timed_out": 0,
            "retried": 0,
        }

    @property
    def max_parallel(self) -> int:
        return int(self._settings.get("max_parallel_jobs", "20"))

    @property
    def job_timeout(self) -> int:
        return int(self._settings.get("job_heartbeat_timeout_seconds", "120")) * 5

    @property
    def heartbeat_timeout(self) -> int:
        return int(self._settings.get("job_heartbeat_timeout_seconds", "120"))

    @property
    def poll_interval(self) -> float:
        return 5.0

    async def _run_worker(self, job: dict[str, Any]) -> None:
        """Run a single job through the worker pipeline.

        The launcher itself does not execute the job. This method is the
        boundary where a real worker (Appium, device automation, etc.) would
        be invoked. For now it transitions the job to preparing_device and
        emits a heartbeat loop — the actual device/posting work is handled
        by a separate worker process or will be wired in a follow-up issue.
        """
        job_id = str(job["id"])
        _jsonl("worker_start", job_id=job_id)

        async with await psycopg.AsyncConnection.connect(
            self.db_url, autocommit=True
        ) as conn:
            await _transition_job(conn, job_id, "preparing_device")
            _jsonl("job_status", job_id=job_id, status="preparing_device")

            # Heartbeat: keep updated_at fresh while the job is active.
            # A real worker replaces this loop with actual device automation
            # and calls _update_heartbeat between steps.
            while not self._shutdown.is_set():
                await _update_heartbeat(conn, job_id)
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=self.heartbeat_timeout / 4
                    )
                    break
                except asyncio.TimeoutError:
                    pass

    async def _dispatch(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        attempt = 0
        while True:
            try:
                async with self._semaphore:
                    await asyncio.wait_for(
                        self._run_worker(job), timeout=self.job_timeout
                    )
                _jsonl("worker_done", job_id=job_id)
                self.stats["done"] += 1
                return
            except asyncio.TimeoutError:
                _jsonl("worker_timeout", job_id=job_id)
                self.stats["timed_out"] += 1
                async with await psycopg.AsyncConnection.connect(
                    self.db_url, autocommit=True
                ) as conn:
                    await _transition_job(
                        conn,
                        job_id,
                        "failed",
                        error_code="TIMEOUT",
                        error_message=f"Job exceeded {self.job_timeout}s timeout",
                    )
                    await _release_device(conn, job_id)
                return
            except INFRA_ERRORS as exc:
                attempt += 1
                if attempt > MAX_INFRA_RETRIES:
                    _jsonl(
                        "worker_failed",
                        job_id=job_id,
                        error=str(exc),
                        attempts=attempt,
                    )
                    self.stats["failed"] += 1
                    try:
                        async with await psycopg.AsyncConnection.connect(
                            self.db_url, autocommit=True
                        ) as conn:
                            await _transition_job(
                                conn,
                                job_id,
                                "failed",
                                error_code="INFRA",
                                error_message=str(exc),
                            )
                            await _release_device(conn, job_id)
                    except Exception:
                        _jsonl("cleanup_error", job_id=job_id)
                    return
                self.stats["retried"] += 1
                delay = INFRA_RETRY_BACKOFF * attempt
                _jsonl(
                    "worker_retry",
                    job_id=job_id,
                    attempt=attempt,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                _jsonl("worker_cancelled", job_id=job_id)
                raise
            except Exception as exc:
                _jsonl("worker_failed", job_id=job_id, error=str(exc))
                self.stats["failed"] += 1
                try:
                    async with await psycopg.AsyncConnection.connect(
                        self.db_url, autocommit=True
                    ) as conn:
                        await _transition_job(
                            conn,
                            job_id,
                            "failed",
                            error_code="UNKNOWN",
                            error_message=str(exc),
                        )
                        await _release_device(conn, job_id)
                except Exception:
                    _jsonl("cleanup_error", job_id=job_id)
                return

    async def _heartbeat_monitor(self) -> None:
        """Detect stale jobs that stopped sending heartbeats."""
        while not self._shutdown.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.db_url, autocommit=True
                ) as conn:
                    stale_ids = await _detect_stale_jobs(conn, self.heartbeat_timeout)
                    for job_id in stale_ids:
                        _jsonl("stale_job_detected", job_id=job_id)
                        await _transition_job(
                            conn,
                            job_id,
                            "needs_review",
                            error_code="HEARTBEAT_TIMEOUT",
                            error_message=f"No heartbeat for {self.heartbeat_timeout}s",
                        )
                        await _release_device(conn, job_id)
                        self.stats["timed_out"] += 1
                        task = self._active.pop(str(job_id), None)
                        if task and not task.done():
                            task.cancel()
            except INFRA_ERRORS as exc:
                _jsonl("heartbeat_monitor_error", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self.heartbeat_timeout / 2
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _scheduler_loop(self) -> None:
        """Main loop: create jobs and dispatch workers."""
        while not self._shutdown.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    self.db_url, autocommit=True
                ) as conn:
                    active = await _count_active_jobs(conn)
                    capacity = self.max_parallel - active
                    if capacity <= 0:
                        _jsonl("at_capacity", active=active, max=self.max_parallel)
                    else:
                        created_this_round = 0
                        for _ in range(capacity):
                            job = await _create_publishing_job(conn)
                            if job is None:
                                break
                            job_id = str(job["id"])
                            self.stats["created"] += 1
                            _jsonl("job_created", job_id=job_id)
                            task = asyncio.create_task(
                                self._dispatch(job), name=f"job-{job_id[:8]}"
                            )
                            self._active[job_id] = task
                            task.add_done_callback(
                                lambda t, jid=job_id: self._active.pop(jid, None)
                            )
                            created_this_round += 1
                        if created_this_round == 0:
                            _jsonl("no_resources")
            except INFRA_ERRORS as exc:
                _jsonl("scheduler_loop_error", error=str(exc))

            # Clean up finished tasks
            for jid in list(self._active):
                task = self._active.get(jid)
                if task and task.done():
                    del self._active[jid]

            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self.poll_interval
                )
                break
            except asyncio.TimeoutError:
                pass

    def _print_summary(self) -> None:
        _jsonl("summary", **self.stats)
        log.info(
            "summary: created=%d done=%d failed=%d timed_out=%d retried=%d",
            self.stats["created"],
            self.stats["done"],
            self.stats["failed"],
            self.stats["timed_out"],
            self.stats["retried"],
        )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown.set)

        _jsonl("launcher_start")
        log.info("connecting to database")

        async with await psycopg.AsyncConnection.connect(
            self.db_url, autocommit=True
        ) as conn:
            self._settings = await _load_settings(conn)

        self._semaphore = asyncio.Semaphore(self.max_parallel)
        _jsonl("settings_loaded", max_parallel=self.max_parallel, job_timeout=self.job_timeout, heartbeat_timeout=self.heartbeat_timeout)
        log.info(
            "max_parallel=%d job_timeout=%ds heartbeat_timeout=%ds",
            self.max_parallel,
            self.job_timeout,
            self.heartbeat_timeout,
        )

        monitor = asyncio.create_task(self._heartbeat_monitor(), name="heartbeat-monitor")
        scheduler = asyncio.create_task(self._scheduler_loop(), name="scheduler-loop")

        try:
            await asyncio.gather(scheduler, monitor)
        except asyncio.CancelledError:
            pass
        finally:
            self._shutdown.set()
            # Wait for in-flight workers to finish (with a grace period)
            if self._active:
                _jsonl("draining", active=len(self._active))
                log.info("draining %d active job(s)", len(self._active))
                _, pending = await asyncio.wait(
                    self._active.values(), timeout=30
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=5)

            monitor.cancel()
            self._print_summary()
            _jsonl("launcher_stop")
