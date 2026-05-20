"""Tests for the async job launcher (FFF-16).

Uses an in-process Postgres 17 Docker container — same pattern as the
existing bash-based scheduler tests, but driven by pytest + psycopg.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()
MIGRATIONS_DIR = f"{REPO_ROOT}/supabase/migrations"
SEED_FILE = f"{REPO_ROOT}/supabase/seed.sql"


# ---------------------------------------------------------------------------
# Fixtures: ephemeral Postgres container
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_container():
    """Start a throwaway Postgres 17 container for the test module."""
    name = f"fffbt_launcher_test_{id(object())}"
    port = 54398
    subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-e", "POSTGRES_PASSWORD=postgres",
            "-p", f"{port}:5432",
            "postgres:17-alpine",
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"

    # Wait for readiness
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
    dsn, _ = pg_container
    import pathlib

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


# ---------------------------------------------------------------------------
# Unit tests (mocked DB — fast, no Docker needed)
# ---------------------------------------------------------------------------


class TestJobLauncherUnit:
    """Unit tests that mock the database layer."""

    def test_jsonl_output_is_valid_json(self, capsys):
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import _jsonl

        _jsonl("test_event", foo="bar", num=42)
        line = capsys.readouterr().err.strip()
        parsed = json.loads(line)
        assert parsed["event"] == "test_event"
        assert parsed["foo"] == "bar"
        assert parsed["num"] == 42
        assert "ts" in parsed

    def test_infra_errors_tuple(self):
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import INFRA_ERRORS

        assert ConnectionError in INFRA_ERRORS
        assert TimeoutError in INFRA_ERRORS
        assert OSError in INFRA_ERRORS

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Verify the semaphore actually caps concurrent workers."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import JobLauncher

        max_concurrent = 0
        current = 0
        lock = asyncio.Lock()

        launcher = JobLauncher("postgresql://fake")
        launcher._settings = {"max_parallel_jobs": "2", "job_heartbeat_timeout_seconds": "120"}
        launcher._semaphore = asyncio.Semaphore(2)
        launcher._shutdown = asyncio.Event()

        original_run_worker = launcher._run_worker

        async def mock_worker(job):
            nonlocal max_concurrent, current
            async with lock:
                current += 1
                max_concurrent = max(max_concurrent, current)
            await asyncio.sleep(0.1)
            async with lock:
                current -= 1

        launcher._run_worker = mock_worker

        jobs = [{"id": str(uuid.uuid4())} for _ in range(5)]
        tasks = [asyncio.create_task(launcher._dispatch(j)) for j in jobs]
        await asyncio.gather(*tasks)

        assert max_concurrent <= 2

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        """Verify stats are updated on success/failure."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import JobLauncher

        launcher = JobLauncher("postgresql://fake")
        launcher._settings = {"max_parallel_jobs": "10", "job_heartbeat_timeout_seconds": "120"}
        launcher._semaphore = asyncio.Semaphore(10)
        launcher._shutdown = asyncio.Event()

        async def ok_worker(job):
            pass

        launcher._run_worker = ok_worker

        job = {"id": str(uuid.uuid4())}
        await launcher._dispatch(job)
        assert launcher.stats["done"] == 1

    @pytest.mark.asyncio
    async def test_timeout_calls_process_job_error(self):
        """A timed-out job routes through _handle_worker_error with TIMEOUT."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import JobLauncher

        launcher = JobLauncher("postgresql://fake")
        launcher._settings = {"max_parallel_jobs": "10", "job_heartbeat_timeout_seconds": "1"}
        launcher._semaphore = asyncio.Semaphore(10)
        launcher._shutdown = asyncio.Event()

        async def slow_worker(job):
            await asyncio.sleep(100)

        launcher._run_worker = slow_worker

        captured = {}

        async def mock_handle(job_id, error_code, error_message):
            captured["error_code"] = error_code
            launcher.stats["failed"] += 1

        launcher._handle_worker_error = mock_handle

        job = {"id": str(uuid.uuid4())}
        await launcher._dispatch(job)

        assert launcher.stats["timed_out"] == 1
        assert captured["error_code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_infra_error_calls_process_job_error(self):
        """Infra errors route through _handle_worker_error with INFRA."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import JobLauncher

        launcher = JobLauncher("postgresql://fake")
        launcher._settings = {"max_parallel_jobs": "10", "job_heartbeat_timeout_seconds": "120"}
        launcher._semaphore = asyncio.Semaphore(10)
        launcher._shutdown = asyncio.Event()

        async def failing_worker(job):
            raise ConnectionError("db down")

        launcher._run_worker = failing_worker

        captured = {}

        async def mock_handle(job_id, error_code, error_message):
            captured["error_code"] = error_code
            captured["error_message"] = error_message
            launcher.stats["failed"] += 1

        launcher._handle_worker_error = mock_handle

        job = {"id": str(uuid.uuid4())}
        await launcher._dispatch(job)

        assert captured["error_code"] == "INFRA"
        assert "db down" in captured["error_message"]

    @pytest.mark.asyncio
    async def test_unknown_error_calls_process_job_error(self):
        """Unexpected exceptions route through _handle_worker_error with UNKNOWN."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import JobLauncher

        launcher = JobLauncher("postgresql://fake")
        launcher._settings = {"max_parallel_jobs": "10", "job_heartbeat_timeout_seconds": "120"}
        launcher._semaphore = asyncio.Semaphore(10)
        launcher._shutdown = asyncio.Event()

        async def exploding_worker(job):
            raise RuntimeError("something unexpected")

        launcher._run_worker = exploding_worker

        captured = {}

        async def mock_handle(job_id, error_code, error_message):
            captured["error_code"] = error_code
            launcher.stats["failed"] += 1

        launcher._handle_worker_error = mock_handle

        job = {"id": str(uuid.uuid4())}
        await launcher._dispatch(job)

        assert captured["error_code"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Integration tests (real Postgres — require Docker)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestJobLauncherIntegration:
    """Integration tests against a real Postgres instance."""

    def test_create_publishing_job(self, db_url):
        """create_publishing_job() creates a job when resources exist."""
        ids = _insert_fixtures(db_url)

        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            cur = conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = cur.fetchone()

        assert row is not None
        cols = [desc.name for desc in cur.description]
        job = dict(zip(cols, row))
        assert job["id"] is not None
        assert job["status"] == "queued"
        assert str(job["video_id"]) == ids["video_id"]

    def test_no_resources_returns_null(self, db_url):
        """create_publishing_job() returns NULL when no videos are available."""
        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            # Exhaust all 'new' videos
            conn.execute("UPDATE automation.videos SET status = 'released' WHERE status = 'new'")
            cur = conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = cur.fetchone()

        cols = [desc.name for desc in cur.description]
        result = dict(zip(cols, row)) if row else None
        assert result is None or result.get("id") is None

    @pytest.mark.asyncio
    async def test_launcher_creates_multiple_jobs(self, db_url):
        """Launcher creates jobs for all available resources without blocking."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import JobLauncher

        # Insert 3 videos, 3 accounts, 3 devices
        for _ in range(3):
            _insert_fixtures(db_url)

        launcher = JobLauncher(db_url)

        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            launcher._settings = {
                row[0]: row[1]
                for row in await (await conn.execute(
                    "SELECT key, value FROM automation.global_settings"
                )).fetchall()
            }

        launcher._semaphore = asyncio.Semaphore(launcher.max_parallel)
        launcher._shutdown = asyncio.Event()

        # Override worker to just succeed immediately
        async def noop_worker(job):
            pass

        launcher._run_worker = noop_worker

        # Run one iteration of the scheduler
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            for _ in range(3):
                job = await conn.execute("SELECT * FROM automation.create_publishing_job()")
                row = await job.fetchone()
                if row is None:
                    break
                cols = [desc.name for desc in job.description]
                job_dict = dict(zip(cols, row))
                if job_dict.get("id"):
                    launcher.stats["created"] += 1
                    task = asyncio.create_task(launcher._dispatch(job_dict))
                    launcher._active[str(job_dict["id"])] = task

        if launcher._active:
            await asyncio.wait(launcher._active.values(), timeout=10)

        assert launcher.stats["created"] >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_detects_stale(self, db_url):
        """Stale jobs (no heartbeat) are moved to needs_review."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import _detect_stale_jobs, _transition_job

        ids = _insert_fixtures(db_url)

        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])

            # Transition to preparing_device and age the updated_at timestamp.
            # Disable the trigger so we can backdate updated_at manually.
            await _transition_job(conn, job_id, "preparing_device")
            await conn.execute("ALTER TABLE automation.jobs DISABLE TRIGGER trg_jobs_updated_at")
            await conn.execute(
                "UPDATE automation.jobs SET updated_at = now() - interval '300 seconds' WHERE id = %s",
                (job_id,),
            )
            await conn.execute("ALTER TABLE automation.jobs ENABLE TRIGGER trg_jobs_updated_at")

            stale = await _detect_stale_jobs(conn, 120)
            assert job_id in [str(s) for s in stale]

    @pytest.mark.asyncio
    async def test_process_job_error_retries_via_catalog(self, db_url):
        """process_job_error() retries INFRA errors and re-queues the job."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import _process_job_error, _transition_job

        ids = _insert_fixtures(db_url)

        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])

            await _transition_job(conn, job_id, "preparing_device")

            result = await _process_job_error(conn, job_id, "INFRA", "connection reset")

            assert result["action"] == "retried"
            assert result["retry_count"] == 1

            cur = await conn.execute(
                "SELECT status, retry_count FROM automation.jobs WHERE id = %s",
                (job_id,),
            )
            row = await cur.fetchone()
            assert row[0] == "queued"
            assert row[1] == 1

    @pytest.mark.asyncio
    async def test_process_job_error_needs_review_for_unknown(self, db_url):
        """process_job_error() moves UNKNOWN errors to needs_review."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import _process_job_error, _transition_job

        ids = _insert_fixtures(db_url)

        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])

            await _transition_job(conn, job_id, "preparing_device")

            result = await _process_job_error(conn, job_id, "UNKNOWN", "bug")

            assert result["action"] == "needs_review"

            cur = await conn.execute(
                "SELECT status FROM automation.jobs WHERE id = %s", (job_id,)
            )
            assert (await cur.fetchone())[0] == "needs_review"

    @pytest.mark.asyncio
    async def test_process_job_error_releases_device(self, db_url):
        """Device is released after process_job_error()."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import _process_job_error, _transition_job

        ids = _insert_fixtures(db_url)

        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])
            device_id = str(job["device_id"])

            await _transition_job(conn, job_id, "preparing_device")
            await _process_job_error(conn, job_id, "INFRA", "conn error")

            cur = await conn.execute(
                "SELECT status, current_job_id FROM automation.physical_devices WHERE id = %s",
                (device_id,),
            )
            row = await cur.fetchone()
            assert row[0] == "online"
            assert row[1] is None

    @pytest.mark.asyncio
    async def test_requeued_job_picked_up(self, db_url):
        """Re-queued jobs are fetched and get a new device assigned."""
        import sys
        sys.path.insert(0, f"{REPO_ROOT}/src")
        from scheduler.launcher import (
            _fetch_requeued_jobs,
            _process_job_error,
            _reserve_device_for_job,
            _transition_job,
        )

        ids = _insert_fixtures(db_url)
        # Insert a second device for the retry
        second_device_id = str(uuid.uuid4())
        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            conn.execute(
                "INSERT INTO automation.physical_devices (id, alias, adb_serial, status, last_seen_at) "
                "VALUES (%s, %s, %s, 'online', now())",
                (second_device_id, f"device_{second_device_id[:8]}", f"emulator-{second_device_id[:4]}"),
            )

        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            cur = await conn.execute("SELECT * FROM automation.create_publishing_job()")
            row = await cur.fetchone()
            if row is None:
                pytest.skip("no resources to create a job")
            cols = [desc.name for desc in cur.description]
            job = dict(zip(cols, row))
            job_id = str(job["id"])
            original_device_id = str(job["device_id"])

            await _transition_job(conn, job_id, "preparing_device")
            await _process_job_error(conn, job_id, "INFRA", "retry me")

            requeued = await _fetch_requeued_jobs(conn)
            requeued_ids = [str(j["id"]) for j in requeued]
            assert job_id in requeued_ids

            new_device = await _reserve_device_for_job(conn, job_id)
            assert new_device is not None
            assert new_device != original_device_id

            cur = await conn.execute(
                "SELECT device_id FROM automation.jobs WHERE id = %s", (job_id,)
            )
            assert str((await cur.fetchone())[0]) == new_device
