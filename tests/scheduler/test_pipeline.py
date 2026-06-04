"""Tests for the generic job runner pipeline (FFF-55).

Unit tests use mocked DB — no Docker needed.
Integration tests require Docker and are marked with @pytest.mark.integration.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import types
import uuid
from unittest.mock import AsyncMock, patch

import psycopg
import pytest

REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()
MIGRATIONS_DIR = f"{REPO_ROOT}/supabase/migrations"
SEED_FILE = f"{REPO_ROOT}/supabase/seed.sql"

import sys

sys.path.insert(0, f"{REPO_ROOT}/src")

from src.worker.steps import (
    MobileUIAutomationStep as RealMobileUIAutomationStep,
    VerificationStep as RealVerificationStep,
    VideoPreparationStep as RealVideoPreparationStep,
)
from scheduler.pipeline import (
    CleanupStep,
    ProofOfPostingEnvironmentStep,
    ProofOfPostingWorkerStep,
    StepContext,
    StepResult,
    StubEnvironmentApplyStep,
    StubMobileUIAutomationStep,
    StubVerificationStep,
    StubVideoPreparationStep,
    _execute_step,
    stub_steps,
    proof_of_posting_steps,
    run_job_pipeline,
    _prepare_caption,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides) -> StepContext:
    defaults = {
        "job_id": str(uuid.uuid4()),
        "video_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "account_environment_id": str(uuid.uuid4()),
        "device_id": str(uuid.uuid4()),
        "settings": {"job_heartbeat_timeout_seconds": "120"},
    }
    defaults.update(overrides)
    return StepContext(**defaults)


class RecordingStep:
    """A step that records execution order for testing."""

    def __init__(self, name: str, result: StepResult | None = None):
        self._name = name
        self._result = result
        self.called = False
        self.call_order = -1

    @property
    def name(self) -> str:
        return self._name

    async def run(self, ctx: StepContext) -> StepResult:
        self.called = True
        if self._result:
            return self._result
        return StepResult(step=self.name, status="ok", message=f"{self.name} ok")


class ExplodingStep:
    """A step that raises an exception."""

    def __init__(self, name: str, error: Exception):
        self._name = name
        self._error = error

    @property
    def name(self) -> str:
        return self._name

    async def run(self, ctx: StepContext) -> StepResult:
        raise self._error


def _make_mock_conn(job_id: str = "job-1"):
    """Create a mock async connection that handles pipeline DB calls."""
    mock_conn = AsyncMock()

    async def _execute(query, params=None):
        cursor = AsyncMock()
        q = str(query)
        if "FROM automation.jobs" in q and "WHERE" in q:
            cursor.fetchone = AsyncMock(
                return_value=(
                    job_id,
                    "vid-1",
                    "acct-1",
                    "env-1",
                    "dev-1",
                    "/tmp/clip.mp4",
                    "instagram/test/videos/",
                    "clip.mp4",
                    "DEVICE001",
                    None,
                    None,
                    None,
                )
            )
            cursor.description = [
                types.SimpleNamespace(name=n)
                for n in [
                    "id",
                    "video_id",
                    "account_id",
                    "environment_id",
                    "device_id",
                    "local_video_path",
                    "source_path",
                    "filename",
                    "adb_serial",
                    "adb_connect_target",
                    "tailscale_ipv4",
                    "genfarmer_device_id",
                ]
            ]
        elif "RETURNING id" in q:
            cursor.fetchone = AsyncMock(return_value=None)
        else:
            cursor.fetchone = AsyncMock(return_value=None)
        return cursor

    mock_conn.execute = AsyncMock(side_effect=_execute)
    return mock_conn


def _patch_connect(mock_conn):
    """Return a context manager that patches psycopg.AsyncConnection.connect."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "scheduler.pipeline.psycopg.AsyncConnection.connect",
        new=AsyncMock(return_value=mock_cm),
    )


# ---------------------------------------------------------------------------
# Type tests
# ---------------------------------------------------------------------------


class TestStepResult:
    def test_defaults(self):
        r = StepResult(step="test", status="ok")
        assert r.code is None
        assert r.message == ""
        assert r.retryable is None
        assert r.warnings == []
        assert r.artifacts == []
        assert r.details is None

    def test_failure_with_code(self):
        r = StepResult(step="test", status="failed", code="INFRA", message="db down")
        assert r.status == "failed"
        assert r.code == "INFRA"

    def test_warnings_and_artifacts(self):
        r = StepResult(
            step="test",
            status="ok",
            warnings=[{"code": "w1", "step": "test", "detail": "minor"}],
            artifacts=[
                {"artifact_id": "a1", "artifact_type": "screenshot", "label": "on_error"}
            ],
        )
        assert len(r.warnings) == 1
        assert len(r.artifacts) == 1


class TestStepContext:
    def test_frozen(self):
        ctx = _make_ctx()
        with pytest.raises(AttributeError):
            ctx.job_id = "new"

    def test_fields(self):
        ctx = _make_ctx(job_id="j1", video_id="v1")
        assert ctx.job_id == "j1"
        assert ctx.video_id == "v1"


# ---------------------------------------------------------------------------
# Stub tests
# ---------------------------------------------------------------------------


class TestStubs:
    @pytest.mark.asyncio
    async def test_all_stubs_return_ok(self):
        ctx = _make_ctx()
        for step_cls in [
            StubEnvironmentApplyStep,
            StubVideoPreparationStep,
            StubMobileUIAutomationStep,
            StubVerificationStep,
            CleanupStep,
        ]:
            step = step_cls()
            result = await step.run(ctx)
            assert result.status == "ok"
            assert result.step == step.name

    def test_stub_steps_order(self):
        steps = stub_steps()
        assert len(steps) == 4
        names = [s.name for s in steps]
        assert names == [
            "environment_apply",
            "video_preparation",
            "mobile_ui_automation",
            "verification",
        ]
        # All stub steps are unmistakably named Stub*.
        assert all(type(s).__name__.startswith("Stub") for s in steps)


class TestProofOfPostingSteps:
    def test_proof_of_posting_steps_are_real_worker_steps(self):
        steps = proof_of_posting_steps()

        assert [s.name for s in steps] == [
            "environment_apply",
            "video_preparation",
            "mobile_ui_automation",
            "verification",
        ]
        assert isinstance(steps[0], ProofOfPostingEnvironmentStep)
        assert isinstance(steps[1], ProofOfPostingWorkerStep)
        assert isinstance(steps[1].implementation, RealVideoPreparationStep)
        assert isinstance(steps[2].implementation, RealMobileUIAutomationStep)
        assert isinstance(steps[3].implementation, RealVerificationStep)
        assert not any(
            isinstance(s, (StubEnvironmentApplyStep, StubVideoPreparationStep,
                           StubMobileUIAutomationStep, StubVerificationStep))
            for s in steps
        )

    @pytest.mark.asyncio
    async def test_environment_step_does_not_mutate_environment(self):
        result = await ProofOfPostingEnvironmentStep().run(_make_ctx())

        assert result.status == "ok"
        assert result.details == {
            "mode": "proof_of_posting",
            "mutates_environment": False,
        }
        assert "ChangeInfo" in result.message


class TestCaptionAssembly:
    def test_prepare_caption_writes_non_placeholder_caption_and_hashtags(self):
        conn = AsyncMock()
        ctx = _make_ctx(settings={"job_heartbeat_timeout_seconds": "120"})

        hashtags = asyncio.run(_prepare_caption(conn, ctx))

        caption = ctx.settings["caption_text"]
        assert "football" in caption.lower() or "fifa" in caption.lower()
        assert 3 <= len(hashtags) <= 7
        # hashtags must be stored as a real list[str], not a joined string —
        # a joined string gets re-split per-character by the agent runner.
        assert ctx.settings["hashtags"] == list(hashtags)
        assert isinstance(ctx.settings["hashtags"], list)
        # caption_base is the body without tags; the full caption starts with it.
        assert caption.startswith(ctx.settings["caption_base"])
        update_call = conn.execute.await_args_list[0]
        assert "UPDATE automation.jobs SET caption" in update_call.args[0]
        assert update_call.args[1][0] == caption
        assert update_call.args[1][1] == hashtags

    def test_prepare_caption_honors_requested_hashtag_range(self):
        conn = AsyncMock()
        ctx = _make_ctx(
            settings={
                "job_heartbeat_timeout_seconds": "120",
                "caption_text": "Football fans are ready.",
                "hashtag_count": "3",
            }
        )

        hashtags = asyncio.run(_prepare_caption(conn, ctx))

        assert ctx.settings["caption_text"].startswith("Football fans are ready.")
        assert len(hashtags) == 3


# ---------------------------------------------------------------------------
# _execute_step tests
# ---------------------------------------------------------------------------


class TestExecuteStep:
    @pytest.mark.asyncio
    async def test_successful_step(self):
        conn = AsyncMock()
        ctx = _make_ctx()
        step = RecordingStep("test")
        shutdown = asyncio.Event()

        result = await _execute_step(conn, ctx, step, 30.0, shutdown)

        assert result.status == "ok"
        assert step.called

    @pytest.mark.asyncio
    async def test_exception_wrapped_as_failed(self):
        conn = AsyncMock()
        ctx = _make_ctx()
        step = ExplodingStep("test", RuntimeError("boom"))
        shutdown = asyncio.Event()

        result = await _execute_step(conn, ctx, step, 30.0, shutdown)

        assert result.status == "failed"
        assert result.code == "UNKNOWN"
        assert "boom" in result.message

    @pytest.mark.asyncio
    async def test_heartbeat_called_during_slow_step(self):
        conn = AsyncMock()
        ctx = _make_ctx()
        shutdown = asyncio.Event()

        class SlowStep:
            name = "slow"

            async def run(self, ctx):
                await asyncio.sleep(0.15)
                return StepResult(step=self.name, status="ok", message="done")

        result = await _execute_step(conn, ctx, SlowStep(), 0.05, shutdown)

        assert result.status == "ok"
        hb_calls = [
            c
            for c in conn.execute.call_args_list
            if "updated_at" in str(c)
        ]
        assert len(hb_calls) >= 2

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        conn = AsyncMock()
        ctx = _make_ctx()
        shutdown = asyncio.Event()

        class CancellingStep:
            name = "canceller"

            async def run(self, ctx):
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _execute_step(conn, ctx, CancellingStep(), 30.0, shutdown)


# ---------------------------------------------------------------------------
# Pipeline orchestration tests (mocked DB)
# ---------------------------------------------------------------------------


class TestRequiresExplicitSteps:
    """run_job_pipeline refuses to run without an explicit step list, so no
    caller can silently fall back to no-op stubs."""

    @pytest.mark.asyncio
    async def test_none_steps_raises_before_db(self):
        with pytest.raises(ValueError, match="explicit non-empty steps"):
            await run_job_pipeline(
                db_url="postgresql://should-not-connect",
                job={"id": str(uuid.uuid4())},
                settings={},
                shutdown=asyncio.Event(),
                steps=None,
            )

    @pytest.mark.asyncio
    async def test_empty_steps_raises_before_db(self):
        with pytest.raises(ValueError, match="explicit non-empty steps"):
            await run_job_pipeline(
                db_url="postgresql://should-not-connect",
                job={"id": str(uuid.uuid4())},
                settings={},
                shutdown=asyncio.Event(),
                steps=[],
            )


class TestPipelineOrchestration:
    @pytest.mark.asyncio
    async def test_all_steps_ok_transitions_to_done(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        steps = [
            RecordingStep("environment_apply"),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        for s in steps:
            assert s.called, f"{s.name} should have been called"

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("preparing_device" in c for c in calls)
        assert any("publishing" in c for c in calls)
        assert any("verifying" in c for c in calls)
        assert any("done" in c for c in calls)

    @pytest.mark.asyncio
    async def test_step_failure_stops_pipeline(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        fail_result = StepResult(
            step="video_preparation",
            status="failed",
            code="upload_failed",
            message="upload timed out",
        )
        steps = [
            RecordingStep("environment_apply"),
            RecordingStep("video_preparation", result=fail_result),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        assert steps[0].called
        assert steps[1].called
        assert not steps[2].called
        assert not steps[3].called

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("process_job_error" in c for c in calls)
        assert not any(
            "transition_job_status" in c and "done" in c for c in calls
        )

    @pytest.mark.asyncio
    async def test_skipped_events_written(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        fail_result = StepResult(
            step="environment_apply",
            status="failed",
            code="proxy_failed",
            message="proxy down",
        )
        steps = [
            RecordingStep("environment_apply", result=fail_result),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        event_calls = [
            str(c)
            for c in mock_conn.execute.call_args_list
            if "job_events" in str(c) and "skipped" in str(c)
        ]
        assert len(event_calls) == 3

    @pytest.mark.asyncio
    async def test_cleanup_always_runs(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        fail_result = StepResult(
            step="environment_apply",
            status="failed",
            code="proxy_failed",
            message="proxy down",
        )
        steps = [
            RecordingStep("environment_apply", result=fail_result),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        cleanup_events = [c for c in calls if "job_events" in c and "cleanup" in c]
        assert len(cleanup_events) >= 1

    @pytest.mark.asyncio
    async def test_device_released(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        steps = [
            RecordingStep("environment_apply"),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        release_calls = [c for c in calls if "current_job_id" in c and "online" in c]
        assert len(release_calls) >= 1

    @pytest.mark.asyncio
    async def test_shutdown_stops_pipeline(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        steps = [
            RecordingStep("environment_apply"),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]
        shutdown = asyncio.Event()
        shutdown.set()

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=shutdown,
                steps=steps,
            )

        for s in steps:
            assert not s.called

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert not any(
            "transition_job_status" in c and "done" in c for c in calls
        )

    @pytest.mark.asyncio
    async def test_needs_review_routes_through_process_job_error(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        nr_result = StepResult(
            step="verification",
            status="needs_review",
            code="unknown_screen",
            message="unrecognized Instagram screen",
        )
        steps = [
            RecordingStep("environment_apply"),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification", result=nr_result),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("process_job_error" in c for c in calls)

    @pytest.mark.asyncio
    async def test_step_exception_wrapped_and_routed(self):
        job_id = str(uuid.uuid4())
        mock_conn = _make_mock_conn(job_id)

        steps = [
            ExplodingStep("environment_apply", RuntimeError("crash")),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        with _patch_connect(mock_conn):
            await run_job_pipeline(
                db_url="postgresql://fake",
                job={"id": job_id},
                settings={"job_heartbeat_timeout_seconds": "120"},
                shutdown=asyncio.Event(),
                steps=steps,
            )

        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("process_job_error" in c for c in calls)
        assert not any(
            "transition_job_status" in c and "done" in c for c in calls
        )


# ---------------------------------------------------------------------------
# Integration tests (real Postgres — require Docker)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container():
    """Start a throwaway Postgres 17 container for the test module."""
    name = f"fffbt_pipeline_test_{id(object())}"
    port = 54399
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-p",
            f"{port}:5432",
            "postgres:17-alpine",
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"

    for _ in range(30):
        try:
            with psycopg.connect(dsn) as conn:
                conn.execute("SELECT 1")
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail("Postgres container did not become ready")

    yield dsn, name

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def db_url(pg_container):
    """Apply migrations + seed, return DSN."""
    import pathlib

    dsn, _ = pg_container
    migrations = sorted(pathlib.Path(MIGRATIONS_DIR).glob("*.sql"))
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        for mig in migrations:
            if "_remote_schema" in mig.name:
                continue
            conn.execute(mig.read_text())
        conn.execute(pathlib.Path(SEED_FILE).read_text())
    return dsn


def _insert_fixtures(dsn: str) -> dict:
    """Insert a video, account, environment, and device. Return their IDs."""
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True

        acct_id = str(uuid.uuid4())
        env_id = str(uuid.uuid4())
        proxy_id = str(uuid.uuid4())
        dp_id = str(uuid.uuid4())
        gps_id = str(uuid.uuid4())
        app_id = str(uuid.uuid4())
        video_id = str(uuid.uuid4())
        device_id = str(uuid.uuid4())

        conn.execute(
            "INSERT INTO automation.proxies (id, host, port) VALUES (%s, '127.0.0.1', 8080)",
            (proxy_id,),
        )
        conn.execute(
            "INSERT INTO automation.device_profiles (id, brand, model, android_version, screen_width, screen_height, screen_density) "
            "VALUES (%s, 'Samsung', 'S21', '12', 1080, 2400, 420)",
            (dp_id,),
        )
        conn.execute(
            "INSERT INTO automation.gps_locations (id, label, latitude, longitude) VALUES (%s, 'NYC', 40.7128, -74.0060)",
            (gps_id,),
        )
        conn.execute("INSERT INTO automation.app_states (id) VALUES (%s)", (app_id,))
        conn.execute(
            "INSERT INTO automation.accounts (id, username, password) VALUES (%s, %s, 'pass')",
            (acct_id, f"test_{acct_id[:8]}"),
        )
        conn.execute(
            "INSERT INTO automation.account_environments (id, account_id, proxy_id, device_profile_id, gps_location_id, app_state_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (env_id, acct_id, proxy_id, dp_id, gps_id, app_id),
        )
        conn.execute(
            "INSERT INTO automation.videos (id, source_path, filename, status) VALUES (%s, %s, %s, 'new')",
            (video_id, "instagram/test/videos/", f"test_{video_id[:8]}.mp4"),
        )
        conn.execute(
            "INSERT INTO automation.physical_devices (id, alias, adb_serial, status, last_seen_at) "
            "VALUES (%s, %s, %s, 'online', now())",
            (device_id, f"device_{device_id[:8]}", f"emulator-{device_id[:4]}"),
        )
        return {
            "account_id": acct_id,
            "environment_id": env_id,
            "video_id": video_id,
            "device_id": device_id,
        }


@pytest.mark.integration
class TestPipelineIntegration:
    @pytest.mark.asyncio
    async def test_pipeline_runs_job_to_done(self, db_url):
        """Full pipeline: stub steps take a job from queued to done."""
        _insert_fixtures(db_url)

        async with await psycopg.AsyncConnection.connect(
            db_url, autocommit=True
        ) as conn:
            cur = await conn.execute(
                "SELECT * FROM automation.create_publishing_job()"
            )
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])

        await run_job_pipeline(
            db_url=db_url,
            job=job,
            settings={"job_heartbeat_timeout_seconds": "120"},
            shutdown=asyncio.Event(),
            steps=stub_steps(),
        )

        async with await psycopg.AsyncConnection.connect(
            db_url, autocommit=True
        ) as conn:
            cur = await conn.execute(
                "SELECT status FROM automation.jobs WHERE id = %s", (job_id,)
            )
            assert (await cur.fetchone())[0] == "done"

            cur = await conn.execute(
                "SELECT payload FROM automation.job_events "
                "WHERE job_id = %s AND event_type = 'heartbeat' "
                "ORDER BY created_at",
                (job_id,),
            )
            events = await cur.fetchall()
            step_names = [row[0]["step"] for row in events]
            assert "environment_apply" in step_names
            assert "video_preparation" in step_names
            assert "mobile_ui_automation" in step_names
            assert "verification" in step_names
            assert "cleanup" in step_names

            cur = await conn.execute(
                "SELECT status, current_job_id FROM automation.physical_devices "
                "WHERE id = %s",
                (str(job["device_id"]),),
            )
            device = await cur.fetchone()
            assert device[0] == "online"
            assert device[1] is None

    @pytest.mark.asyncio
    async def test_pipeline_routes_failure(self, db_url):
        """A failing step routes through process_job_error."""
        _insert_fixtures(db_url)

        async with await psycopg.AsyncConnection.connect(
            db_url, autocommit=True
        ) as conn:
            cur = await conn.execute(
                "SELECT * FROM automation.create_publishing_job()"
            )
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])

        fail_result = StepResult(
            step="environment_apply",
            status="failed",
            code="INFRA",
            message="test failure",
        )
        steps = [
            RecordingStep("environment_apply", result=fail_result),
            RecordingStep("video_preparation"),
            RecordingStep("mobile_ui_automation"),
            RecordingStep("verification"),
        ]

        await run_job_pipeline(
            db_url=db_url,
            job=job,
            settings={"job_heartbeat_timeout_seconds": "120"},
            shutdown=asyncio.Event(),
            steps=steps,
        )

        async with await psycopg.AsyncConnection.connect(
            db_url, autocommit=True
        ) as conn:
            cur = await conn.execute(
                "SELECT status, retry_count FROM automation.jobs WHERE id = %s",
                (job_id,),
            )
            row = await cur.fetchone()
            # INFRA is retryable → job should be re-queued
            assert row[0] == "queued"
            assert row[1] >= 1

            cur = await conn.execute(
                "SELECT status, current_job_id FROM automation.physical_devices "
                "WHERE id = %s",
                (str(job["device_id"]),),
            )
            device = await cur.fetchone()
            assert device[0] == "online"
            assert device[1] is None
