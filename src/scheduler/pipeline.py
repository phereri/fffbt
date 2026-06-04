"""Generic job runner pipeline.

Executes a single automation.jobs record through an ordered sequence of
WorkerStep implementations. Each step returns a StepResult; the pipeline
owns all job state transitions, heartbeat emission, and error routing.

Steps are pluggable: callers MUST pass an explicit step list to
run_job_pipeline(). Use proof_of_posting_steps() for real device automation,
or stub_steps() for tests that exercise pipeline mechanics without a device.
There is no implicit default — omitting steps raises, so a real run can never
silently fall back to no-op stubs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import psycopg

from scheduler.hashtags import build_validation_caption
from src.worker.session.types import (
    Mode,
    StepContext as WorkerStepContext,
    StepResult as WorkerStepResult,
)
from src.worker.steps import (
    MobileUIAutomationStep as RealMobileUIAutomationStep,
    VerificationStep as RealVerificationStep,
    VideoPreparationStep as RealVideoPreparationStep,
)


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
# Stub step implementations — TEST-ONLY.
#
# These return OK without touching a device. They are named ``Stub*`` and
# returned only by ``stub_steps()`` so they can never be confused with the
# real ``RealMobileUIAutomationStep`` etc. or selected by accident. Production
# entry points (launcher, run-job) use ``proof_of_posting_steps()``.
# ---------------------------------------------------------------------------


class StubEnvironmentApplyStep:
    name = "environment_apply"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: environment applied")


class StubVideoPreparationStep:
    name = "video_preparation"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: video prepared")


class StubMobileUIAutomationStep:
    name = "mobile_ui_automation"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: mobile automation completed")


class StubVerificationStep:
    name = "verification"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="stub: verification passed")


class CleanupStep:
    name = "cleanup"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(step=self.name, status="ok", message="cleanup completed")


def stub_steps() -> list:
    """Return no-op stub steps. TEST-ONLY — never use against a real queue.

    These return OK without performing any device automation or posting.
    Production callers must use ``proof_of_posting_steps()``.
    """
    return [
        StubEnvironmentApplyStep(),
        StubVideoPreparationStep(),
        StubMobileUIAutomationStep(),
        StubVerificationStep(),
    ]


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _stringify_records(records: list[Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for record in records:
        if hasattr(record, "__dataclass_fields__"):
            result.append({
                key: str(_value(getattr(record, key)))
                for key in record.__dataclass_fields__
            })
        elif isinstance(record, dict):
            result.append({str(k): str(_value(v)) for k, v in record.items()})
        else:
            result.append({"detail": str(record)})
    return result


def _to_pipeline_result(result: WorkerStepResult) -> StepResult:
    return StepResult(
        step=str(_value(result.step)),
        status=str(_value(result.status)),
        code=result.code,
        message=result.message,
        retryable=result.retryable,
        warnings=_stringify_records(result.warnings),
        artifacts=_stringify_records(result.artifacts),
        details=result.details,
    )


def _to_worker_context(ctx: StepContext) -> WorkerStepContext:
    return WorkerStepContext(
        job_id=ctx.job_id,
        video_id=ctx.video_id,
        account_id=ctx.account_id,
        account_environment_id=ctx.account_environment_id,
        device_id=ctx.device_id,
        mode=Mode.PROOF_OF_POSTING,
        settings=ctx.settings,
    )


class ProofOfPostingEnvironmentStep:
    """No-op environment step for already prepared proof-of-posting devices."""

    name = "environment_apply"

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(
            step=self.name,
            status="ok",
            message=(
                "proof_of_posting: device environment is pre-prepared; "
                "ChangeInfo, proxy, and profile mutation skipped"
            ),
            details={
                "mode": "proof_of_posting",
                "mutates_environment": False,
            },
        )


class ProofOfPostingWorkerStep:
    """Adapter from worker step implementations to scheduler pipeline steps."""

    def __init__(self, step: Any) -> None:
        self._step = step

    @property
    def name(self) -> str:
        return str(_value(self._step.name))

    @property
    def implementation(self) -> Any:
        return self._step

    async def run(self, ctx: StepContext) -> StepResult:
        worker_ctx = _to_worker_context(ctx)
        serial = ctx.settings.get("device_serial")

        if isinstance(self._step, RealVideoPreparationStep):
            if not serial:
                return StepResult(
                    step=self.name,
                    status="failed",
                    code="INFRA",
                    message="no device_serial provided",
                )
            result = await self._step.run(
                worker_ctx,
                video_url=ctx.settings.get("video_url"),
                local_video_path=ctx.settings.get("local_video_path"),
                device_serial=serial,
            )
            return _to_pipeline_result(result)

        if isinstance(self._step, RealMobileUIAutomationStep):
            result = await self._step.run(
                worker_ctx,
                device_serial=serial,
                caption_text=ctx.settings.get("caption_text"),
            )
            return _to_pipeline_result(result)

        if isinstance(self._step, RealVerificationStep):
            result = await self._step.run(worker_ctx, device_serial=serial)
            return _to_pipeline_result(result)

        result = await self._step.run(worker_ctx)
        return _to_pipeline_result(result)


def proof_of_posting_steps() -> list:
    """Return real proof-of-posting steps; no production env mutation."""
    return [
        ProofOfPostingEnvironmentStep(),
        ProofOfPostingWorkerStep(RealVideoPreparationStep()),
        ProofOfPostingWorkerStep(RealMobileUIAutomationStep()),
        ProofOfPostingWorkerStep(RealVerificationStep()),
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
        "SELECT j.id, j.video_id, j.account_id, j.environment_id, j.device_id, "
        "v.local_video_path, v.source_path, v.filename, "
        "pd.adb_serial, pd.adb_connect_target, pd.tailscale_ipv4, "
        "pd.genfarmer_device_id "
        "FROM automation.jobs j "
        "JOIN automation.videos v ON v.id = j.video_id "
        "JOIN automation.physical_devices pd ON pd.id = j.device_id "
        "WHERE j.id = %s",
        (job_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise ValueError(f"job not found: {job_id}")
    cols = [d.name for d in cur.description]
    job = dict(zip(cols, row))
    runtime_settings = dict(settings)
    device_serial = (
        job.get("adb_serial")
        or job.get("adb_connect_target")
        or job.get("tailscale_ipv4")
        or job.get("genfarmer_device_id")
    )
    if device_serial:
        runtime_settings.setdefault("device_serial", str(device_serial))
    if job.get("local_video_path"):
        runtime_settings.setdefault("local_video_path", str(job["local_video_path"]))
    if job.get("source_path"):
        runtime_settings.setdefault("video_source_path", str(job["source_path"]))
    if job.get("filename"):
        runtime_settings.setdefault("video_filename", str(job["filename"]))
    return StepContext(
        job_id=str(job["id"]),
        video_id=str(job["video_id"]),
        account_id=str(job["account_id"]),
        account_environment_id=str(job["environment_id"]),
        device_id=str(job["device_id"]),
        settings=runtime_settings,
    )


async def _prepare_caption(
    conn: psycopg.AsyncConnection,
    ctx: StepContext,
) -> list[str]:
    """Select hashtags, assemble full caption, and inject into ctx.settings.

    Returns the selected hashtags list. The assembled caption is written to
    ``ctx.settings["caption_text"]`` (mutates the dict in-place) and the
    hashtags are recorded in a job event for audit.
    """
    requested_caption = ctx.settings.get("caption_text", "")
    hashtag_count = int(ctx.settings.get("hashtag_count", "5"))
    full_caption, hashtags, base_caption = build_validation_caption(
        requested_caption,
        hashtag_count=hashtag_count,
    )
    ctx.settings["caption_text"] = full_caption
    # Store the caption body (no hashtags) and the hashtags as a real list.
    # The deterministic executor types ``caption_text`` (body + tags) directly;
    # the agent executor renders ``caption_base`` + ``hashtags`` so the tags are
    # appended exactly once. Keeping ``hashtags`` a list[str] is mandatory — a
    # joined string was being re-split per-character downstream.
    ctx.settings["caption_base"] = base_caption
    ctx.settings["hashtags"] = list(hashtags)

    await conn.execute(
        "UPDATE automation.jobs SET caption = %s, hashtags = %s WHERE id = %s",
        (full_caption, hashtags, ctx.job_id),
    )
    await conn.execute(
        "INSERT INTO automation.job_events (job_id, event_type, payload) "
        "VALUES (%s, 'heartbeat', %s::jsonb)",
        (
            ctx.job_id,
            json.dumps({
                "step": "caption_assembly",
                "status": "ok",
                "base_caption": base_caption,
                "hashtags": hashtags,
                "full_caption": full_caption,
            }),
        ),
    )
    return hashtags


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
    steps: list,
) -> None:
    """Execute a job through the full worker pipeline.

    ``steps`` is required and must be non-empty — pass
    ``proof_of_posting_steps()`` for real runs or ``stub_steps()`` for tests.
    There is intentionally no implicit default: a real run can never silently
    fall back to no-op stubs.

    Runs the ordered step sequence, always runs cleanup, then routes
    the outcome through process_job_error() or transitions to done.

    Infrastructure-level exceptions (DB failures, etc.) propagate
    to the caller for handling by the launcher's dispatch logic.
    """
    if not steps:
        raise ValueError(
            "run_job_pipeline requires an explicit non-empty steps list: "
            "proof_of_posting_steps() for real device automation, or "
            "stub_steps() for tests. Refusing to run with no steps — there is "
            "no implicit stub fallback."
        )
    job_id = str(job["id"])
    pipeline_steps = steps
    _jsonl("pipeline_start", job_id=job_id)

    async with await psycopg.AsyncConnection.connect(
        db_url, autocommit=True
    ) as conn:
        ctx = await _load_job_context(conn, job_id, settings)
        await _prepare_caption(conn, ctx)
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
