"""Tests for the VPS runtime CLI (FFF-54).

Unit tests for the dispatcher and argument parsing run without a database.
Integration tests for create-job, run-job, and status use an ephemeral
Docker Postgres container — same pattern as test_launcher.py.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time
import uuid

import psycopg
import pytest


REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()
MIGRATIONS_DIR = f"{REPO_ROOT}/supabase/migrations"
SEED_FILE = f"{REPO_ROOT}/supabase/seed.sql"
sys.path.insert(0, f"{REPO_ROOT}/src")

from scheduler.cli import _targeted_create_job_sql


# ---------------------------------------------------------------------------
# Unit tests: dispatcher and argument handling (no DB)
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "--help"],
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"},
        )
        assert result.returncode == 0
        assert "discover-devices" in result.stdout
        assert "status" in result.stdout

    def test_no_args_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli"],
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"},
        )
        assert result.returncode == 2

    def test_unknown_command(self):
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "bogus"],
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"},
        )
        assert result.returncode == 2
        assert "unknown command" in result.stderr

    def test_create_job_missing_db_url(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "SUPABASE_DB_URL"}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "create-job"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "SUPABASE_DB_URL" in result.stderr
        assert "--via-management-api" in result.stderr

    def test_status_missing_db_url(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "SUPABASE_DB_URL"}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "status"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "SUPABASE_DB_URL" in result.stderr
        assert "--via-management-api" in result.stderr

    def test_via_management_api_missing_pat(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k not in ("SUPABASE_DB_URL", "SUPABASE_PAT")}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "create-job",
             "--via-management-api", "--project-ref", "test-ref"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "SUPABASE_PAT" in result.stderr

    def test_via_management_api_missing_project_ref(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k not in ("SUPABASE_DB_URL", "SUPABASE_PROJECT_REF")}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        env["SUPABASE_PAT"] = "sbp_test_token"
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "create-job",
             "--via-management-api"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "project-ref" in result.stderr

    def test_create_job_account_id_requires_device_serial(self):
        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        env["SUPABASE_PAT"] = "sbp_test_token"
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli", "create-job",
                "--via-management-api", "--project-ref", "test-ref",
                "--account-id", "00000000-0000-0000-0000-000000000000",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "--account-id requires --device-serial" in result.stderr

    def test_create_job_rejects_invalid_account_id(self):
        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        env["SUPABASE_PAT"] = "sbp_test_token"
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli", "create-job",
                "--via-management-api", "--project-ref", "test-ref",
                "--device-serial", "serial-1",
                "--account-id", "not-a-uuid",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "--account-id must be a UUID" in result.stderr

    def test_targeted_create_job_sql_is_self_contained(self):
        sql = _targeted_create_job_sql("100.110.232.89:5555")
        assert "find_eligible_account" not in sql
        assert "reserve_next_video" not in sql
        assert "reserve_physical_device" not in sql
        assert "automation.accounts" in sql
        assert "automation.physical_devices" in sql
        assert "100.110.232.89:5555" in sql

    def test_run_launcher_rejects_management_api(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "SUPABASE_DB_URL"}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        env["SUPABASE_PAT"] = "sbp_test_token"
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "run-launcher",
             "--via-management-api", "--project-ref", "test-ref"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "direct Postgres connection" in result.stderr

    def test_run_job_rejects_management_api(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "SUPABASE_DB_URL"}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        env["SUPABASE_PAT"] = "sbp_test_token"
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "run-job",
             "00000000-0000-0000-0000-000000000000",
             "--via-management-api", "--project-ref", "test-ref"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "direct Postgres connection" in result.stderr

    def test_run_job_accepts_mode_and_json_but_rejects_management_api(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "SUPABASE_DB_URL"}
        env["PYTHONPATH"] = f"{REPO_ROOT}/src"
        env["SUPABASE_PAT"] = "sbp_test_token"
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli", "run-job",
                "00000000-0000-0000-0000-000000000000",
                "--mode", "proof_of_posting",
                "--via-management-api", "--project-ref", "test-ref",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "unrecognized arguments" not in result.stderr
        assert "DIRECT_DB_REQUIRED" in result.stdout

    def test_run_job_proof_of_posting_refuses_stub_pipeline(self):
        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli", "run-job",
                "00000000-0000-0000-0000-000000000000",
                "--mode", "proof_of_posting",
                "--db-url", "postgresql://example.invalid/postgres",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "REAL_WORKER_NOT_WIRED" in result.stdout

    def test_subcommand_help(self):
        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        for cmd in ("create-job", "run-launcher", "run-job", "status"):
            result = subprocess.run(
                [sys.executable, "-m", "scheduler.cli", cmd, "--help"],
                capture_output=True,
                text=True,
                env=env,
            )
            assert result.returncode == 0, f"{cmd} --help failed"


# ---------------------------------------------------------------------------
# Fixtures: ephemeral Postgres container
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container():
    name = f"fffbt_cli_test_{id(object())}"
    port = 54399
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


# ---------------------------------------------------------------------------
# Integration tests: create-job, status (require DB)
# ---------------------------------------------------------------------------


class TestStatusCommand:
    def test_status_text_output(self, db_url: str):
        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        result = subprocess.run(
            [sys.executable, "-m", "scheduler.cli", "status", "--db-url", db_url],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert "Jobs:" in result.stdout
        assert "Devices:" in result.stdout
        assert "Videos:" in result.stdout

    def test_status_json_output(self, db_url: str):
        import json

        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli",
                "status", "--db-url", db_url, "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "jobs" in data
        assert "devices" in data
        assert "videos" in data


class TestCreateJobCommand:
    def test_create_job_no_resources(self, db_url: str):
        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli",
                "create-job", "--db-url", db_url,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 1
        assert "no job created" in result.stdout

    def test_create_job_with_resources(self, db_url: str):
        import json

        with psycopg.connect(db_url) as conn:
            conn.autocommit = True

            proxy_id = str(uuid.uuid4())
            dp_id = str(uuid.uuid4())
            gps_id = str(uuid.uuid4())
            app_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.proxies (id, host, port) "
                "VALUES (%s, '127.0.0.1', 8080)",
                (proxy_id,),
            )
            conn.execute(
                "INSERT INTO automation.device_profiles "
                "(id, brand, model, android_version, "
                "screen_width, screen_height, screen_density) "
                "VALUES (%s, 'Samsung', 'S21', '12', 1080, 2400, 420)",
                (dp_id,),
            )
            conn.execute(
                "INSERT INTO automation.gps_locations "
                "(id, label, latitude, longitude) "
                "VALUES (%s, 'NYC', 40.7128, -74.0060)",
                (gps_id,),
            )
            conn.execute(
                "INSERT INTO automation.app_states (id) VALUES (%s)",
                (app_id,),
            )

            acct_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.accounts "
                "(id, username, password) "
                "VALUES (%s, 'testuser_cli', 'pass')",
                (acct_id,),
            )

            env_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.account_environments "
                "(id, account_id, proxy_id, device_profile_id, "
                "gps_location_id, app_state_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (env_id, acct_id, proxy_id, dp_id, gps_id, app_id),
            )

            vid_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.videos "
                "(id, source_path, filename, status) "
                "VALUES (%s, 'instagram/test/videos/', 'cli_test.mp4', 'new')",
                (vid_id,),
            )

            dev_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.physical_devices "
                "(id, alias, adb_serial, status, last_seen_at) "
                "VALUES (%s, 'cli-test-dev', 'emulator-cli', 'online', now())",
                (dev_id,),
            )

        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli",
                "create-job", "--db-url", db_url, "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["id"] is not None
        assert data["video_id"] is not None

    def test_create_job_with_device_serial_targets_requested_device(self, db_url: str):
        import json

        with psycopg.connect(db_url) as conn:
            conn.autocommit = True

            proxy_id = str(uuid.uuid4())
            dp_id = str(uuid.uuid4())
            gps_id = str(uuid.uuid4())
            app_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.proxies (id, host, port) "
                "VALUES (%s, '127.0.0.1', 8080)",
                (proxy_id,),
            )
            conn.execute(
                "INSERT INTO automation.device_profiles "
                "(id, brand, model, android_version, "
                "screen_width, screen_height, screen_density) "
                "VALUES (%s, 'Samsung', 'S21', '12', 1080, 2400, 420)",
                (dp_id,),
            )
            conn.execute(
                "INSERT INTO automation.gps_locations "
                "(id, label, latitude, longitude) "
                "VALUES (%s, 'NYC', 40.7128, -74.0060)",
                (gps_id,),
            )
            conn.execute(
                "INSERT INTO automation.app_states (id) VALUES (%s)",
                (app_id,),
            )

            acct_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.accounts "
                "(id, username, password) "
                "VALUES (%s, 'targeted_cli_user', 'pass')",
                (acct_id,),
            )

            env_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.account_environments "
                "(id, account_id, proxy_id, device_profile_id, "
                "gps_location_id, app_state_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (env_id, acct_id, proxy_id, dp_id, gps_id, app_id),
            )

            vid_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.videos "
                "(id, source_path, filename, status) "
                "VALUES (%s, 'instagram/test/videos/', 'targeted_cli.mp4', 'new')",
                (vid_id,),
            )

            other_dev_id = str(uuid.uuid4())
            target_dev_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO automation.physical_devices "
                "(id, alias, adb_serial, status, last_seen_at) "
                "VALUES (%s, 'other-dev', 'other:5555', 'online', now())",
                (other_dev_id,),
            )
            conn.execute(
                "INSERT INTO automation.physical_devices "
                "(id, alias, adb_serial, status, last_seen_at) "
                "VALUES (%s, 'target-dev', 'target:5555', 'online', now())",
                (target_dev_id,),
            )

        env = {**dict(__import__("os").environ), "PYTHONPATH": f"{REPO_ROOT}/src"}
        result = subprocess.run(
            [
                sys.executable, "-m", "scheduler.cli",
                "create-job", "--db-url", db_url, "--json",
                "--device-serial", "target:5555",
                "--account-id", acct_id,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["device_id"] == target_dev_id
        assert data["account_id"] == acct_id

        with psycopg.connect(db_url) as conn:
            current = conn.execute(
                "SELECT status, current_job_id FROM automation.physical_devices WHERE id = %s",
                (target_dev_id,),
            ).fetchone()
            assert current[0] == "busy"
            assert str(current[1]) == data["id"]
