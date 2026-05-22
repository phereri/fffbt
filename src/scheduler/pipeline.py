"""Generic job runner pipeline.

Executes a single automation.jobs record through an ordered sequence of
WorkerStep implementations. Each step returns a StepResult; the pipeline
owns all job state transitions, heartbeat emission, and error routing.

Steps are pluggable — pass custom implementations to run_job_pipeline()
or use the default stubs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import psycopg


# ---------------------------------------------------------------------------
# Logging (matches launcher JSONL format)
# ---------------------------------------------------------------------------


def _jsonl(event: str, **kw: Any) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    sys.stderr.write(json.dumps(record, default=str) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepContext:
    """Read-only context built from the jobs row, passed to every step."""

    job_id: str
    video_id: str
    account_id: str
    account_environment_id: str
    device_id: str
    settings: dict[str, str]


@dataclass
class StepResult:
    """Structured output from every WorkerStep."""

    step: str
    status: str  # ok | skipped | failed | needs_review
    code: str | None = None
    message: str = ""
    retryable: bool | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    details: dict[str, Any] | None = None


@runtime_checkable
class WorkerStep(Protocol):
    @property
    def name(self) -> str: ...

    async def run(self, ctx: StepContext) -> StepResult: ...


# ---------------------------------------------------------------------------
# Stub step implementations
# ---------------------------------------------------------------------------


class EnvironmentApplyStep:
    name = "environment_apply"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: environment applied")


class VideoPreparationStep:
    name = "video_preparation"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: video prepared")


class MobileUIAutomationStep:
    name = "mobile_ui_automation"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: mobile automation completed")


class VerificationStep:
    name = "verification"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: verification passed")


class CleanupStep:
    name = "cleanup"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="cleanup completed")


def default_steps() -> list:
    """Return the default ordered pipeline steps (stubs)."""
    return [
        EnvironmentApplyStep(),
        VideoPreparationStep(),
        MobileUIAutomationStep(),
        VerificationStep(),
    ]


# State transition before each step (by step name).
# Steps not listed here run in the current job state.
_PRE_TRANSITIONS: dict[str, str] = {
    "environment_apply": "preparing_device",
    "video_preparation": "publishing",
    "verification": "verifying",
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_job_context(
    conn: psycopg.AsyncConnection, job_id: str, settings: dict[str, str]
) -> StepContext:
    cur = await conn.execute(
        "SELECT id, video_id, account_id, environment_id, device_id "
        "FROM automation.jobs WHERE id = %s",
        (job_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"job not found: {job_id}")
    cols = [d.name for d in cur.description]
    job = dict(zip(cols, row))
    return StepContext(
        job_id=str(job["id"]),
        video_id=str(job["video_id"]),
        account_id=str(job["account_id"]),
        account_environment_id=str(job["environment_id"]),
        device_id=str(job["device_id"]),
        settings=settings,
    )


async def _transition_job(
    conn: psycopg.AsyncConnection,
    job_id: str,
    to_status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    payload: dict[str, str] = {}
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


async def _process_job_error(
    conn: psycopg.AsyncConnection,
    job_id: str,
    error_code: str,
    error_message: str | None = None,
) -> dict[str, Any]:
    cur = await conn.execute(
        "SELECT automation.process_job_error(%s::uuid, %s, %s)",
        (job_id, error_code, error_message),
    )
    row = await cur.fetchone()
    result = row[0] if row and row[0] else {}
    await _release_device(conn, job_id)
    return result


async def _write_step_event(
    conn: psycopg.AsyncConnection, job_id: str, result: StepResult
) -> None:
    payload: dict[str, Any] = {
        "step": result.step,
        "status": result.status,
        "message": result.message,
    }
    if result.code:
        payload["code"] = result.code
    if result.warnings:
        payload["warnings"] = result.warnings
    if result.artifacts:
        payload["artifacts"] = result.artifacts
    if result.details:
        payload["details"] = result.details
    await conn.execute(
        "INSERT INTO automation.job_events (job_id, event_type, payload) "
        "VALUES (%s, 'heartbeat', %s::jsonb)",
        (job_id, json.dumps(payload, default=str)),
    )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def _heartbeat_loop(
    conn: psycopg.AsyncConnection,
    job_id: str,
    interval: float,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await _update_heartbeat(conn, job_id)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------


async def _execute_step(
    conn: psycopg.AsyncConnection,
    ctx: StepContext,
    step: WorkerStep,
    heartbeat_interval: float,
    shutdown: asyncio.Event,
) -> StepResult:
    """Run a single step with a concurrent heartbeat task."""
    await _update_heartbeat(conn, ctx.job_id)

    hb_stop = asyncio.Event()
    hb_task = asyncio.create_task(
        _heartbeat_loop(conn, ctx.job_id, heartbeat_interval, hb_stop)
    )
    try:
        result = await step.run(ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result = StepResult(
            step=step.name, status="failed", code="UNKNOWN", message=str(exc)
        )
    finally:
        hb_stop.set()
        try:
            await hb_task
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


async def run_job_pipeline(
    db_url: str,
    job: dict[str, Any],
    settings: dict[str, str],
    shutdown: asyncio.Event,
    steps: list | None = None,
) -> None:
    """Execute a job through the full worker pipeline.

    Runs the ordered step sequence, always runs cleanup, then routes
    the outcome through process_job_error() or transitions to done.

    Infrastructure-level exceptions (DB failures, etc.) propagate
    to the caller for handling by the launcher's dispatch logic.
    """
    job_id = str(job["id"])
    pipeline_steps = steps if steps is not None else default_steps()
    _jsonl("pipeline_start", job_id=job_id)

    async with await psycopg.AsyncConnection.connect(
        db_url, autocommit=True
    ) as conn:
        ctx = await _load_job_context(conn, job_id, settings)
        heartbeat_interval = (
            int(settings.get("job_heartbeat_timeout_seconds", "120")) / 4
        )

        failing_result: StepResult | None = None

        for i, step in enumerate(pipeline_steps):
            if shutdown.is_set():
                _jsonl("pipeline_shutdown", job_id=job_id, step=step.name)
                break

            pre_transition = _PRE_TRANSITIONS.get(step.name)
            if pre_transition:
                await _transition_job(conn, job_id, pre_transition)
                _jsonl("job_status", job_id=job_id, status=pre_transition)

            _jsonl("step_start", job_id=job_id, step=step.name)
            result = await _execute_step(
                conn, ctx, step, heartbeat_interval, shutdown
            )
            _jsonl(
                "step_done",
                job_id=job_id,
                step=step.name,
                status=result.status,
            )
            await _write_step_event(conn, job_id, result)

            if result.status in ("failed", "needs_review"):
                failing_result = result
                for remaining in pipeline_steps[i + 1 :]:
                    skipped = StepResult(
                        step=remaining.name,
                        status="skipped",
                        message=f"skipped: {step.name} {result.status}",
                    )
                    await _write_step_event(conn, job_id, skipped)
                break

        # Cleanup always runs
        cleanup = CleanupStep()
        _jsonl("step_start", job_id=job_id, step="cleanup")
        try:
            cleanup_result = await cleanup.run(ctx)
        except Exception as exc:
            cleanup_result = StepResult(
                step="cleanup",
                status="failed",
                code="UNKNOWN",
                message=str(exc),
            )
        _jsonl(
            "step_done",
            job_id=job_id,
            step="cleanup",
            status=cleanup_result.status,
        )
        await _write_step_event(conn, job_id, cleanup_result)
        await _release_device(conn, job_id)

        # Route outcome
        if failing_result:
            _jsonl("pipeline_failed", job_id=job_id, code=failing_result.code)
            await _process_job_error(
                conn,
                job_id,
                failing_result.code or "UNKNOWN",
                failing_result.message,
            )
        elif not shutdown.is_set():
            await _transition_job(conn, job_id, "done")
            _jsonl("pipeline_done", job_id=job_id)
