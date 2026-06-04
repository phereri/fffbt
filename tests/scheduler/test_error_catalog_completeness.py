"""Error-catalog completeness + policy tests.

The static tests parse the migration SQL and the worker code with NO database,
guarding the contract that every error code the worker can emit is catalogued
in ``automation.error_catalog``. This matters because
``automation.process_job_error()`` RAISES on an unknown code — an uncatalogued
hard-stop leaves the job stuck and never applies the account side effect.

The integration test (Docker Postgres, ``-m integration``) verifies the runtime
side effects through the real ``process_job_error`` function.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
MIGRATIONS_DIR = Path(REPO_ROOT) / "supabase" / "migrations"
SEED_FILE = Path(REPO_ROOT) / "supabase" / "seed.sql"

from src.worker.agent_runner.mobilerun_agent_runner import _FAILURE_REASON_MAP
from src.worker.steps.mobile_ui_automation import _HARD_STOP_PATTERNS

# Matches a 6-field error_catalog VALUES tuple in declared column order:
# (error_code, category, target_job_status, max_retries, account_side_effect, description)
_ROW_RE = re.compile(
    r"\(\s*'([^']+)'\s*,\s*"
    r"'(retryable|needs_review|non_retryable)'\s*,\s*"
    r"'(failed|needs_review)'\s*,\s*"
    r"(\d+)\s*,\s*"
    r"(NULL|'[^']*')\s*,\s*"
    r"'(?:[^']|'')*'\s*\)",
    re.IGNORECASE,
)


def _parse_catalog() -> dict[str, dict]:
    """Parse every error_catalog row from the migrations into a dict.

    Later migrations override earlier ones (mirrors ON CONFLICT DO UPDATE).
    """
    catalog: dict[str, dict] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        text = path.read_text()
        if "automation.error_catalog" not in text:
            continue
        for m in _ROW_RE.finditer(text):
            code, category, target, max_retries, side = m.groups()
            catalog[code] = {
                "category": category.lower(),
                "target_job_status": target.lower(),
                "max_retries": int(max_retries),
                "account_side_effect": None if side.upper() == "NULL" else side.strip("'"),
            }
    return catalog


def _emitted_codes() -> set[str]:
    """Codes the worker can emit: agent result mapper + deterministic hard stops."""
    codes = {code for code, _category in _FAILURE_REASON_MAP.values()}
    codes |= set(_HARD_STOP_PATTERNS.keys())
    codes.add("unknown_screen")  # map_failure_reason default
    return codes


CATALOG = _parse_catalog()


class TestCatalogParsing:
    def test_parser_finds_known_rows(self):
        # Sanity: the parser actually extracted a non-trivial catalog.
        assert "logged_out" in CATALOG
        assert "INFRA" in CATALOG
        assert len(CATALOG) >= 20


class TestCompleteness:
    def test_every_emitted_code_is_catalogued(self):
        missing = sorted(_emitted_codes() - set(CATALOG))
        assert not missing, (
            f"worker emits codes absent from error_catalog: {missing} — "
            "process_job_error() RAISES on these, leaving jobs stuck."
        )

    @pytest.mark.parametrize("code", sorted(_emitted_codes()))
    def test_no_emitted_code_falls_through_to_default(self, code):
        assert code in CATALOG


class TestAccountSafetyPolicy:
    def test_account_suspended_suspends_account(self):
        row = CATALOG["account_suspended"]
        assert row["account_side_effect"] == "suspended"
        assert row["category"] == "non_retryable"
        assert row["max_retries"] == 0

    def test_login_challenge_does_not_retry_and_disables(self):
        row = CATALOG["login_challenge"]
        assert row["max_retries"] == 0
        assert row["category"] == "non_retryable"
        assert row["account_side_effect"] == "disabled"

    def test_logged_out_disables_account(self):
        assert CATALOG["logged_out"]["account_side_effect"] == "disabled"

    def test_unexpected_destructive_dialog_needs_review(self):
        row = CATALOG["unexpected_destructive_dialog"]
        assert row["target_job_status"] == "needs_review"
        # A bare unexpected dialog is anomalous but not proof the account is bad.
        assert row["account_side_effect"] is None

    def test_final_ok_did_not_register_needs_review(self):
        assert CATALOG["final_ok_did_not_register"]["target_job_status"] == "needs_review"


# ---------------------------------------------------------------------------
# Integration: real process_job_error side effects (Docker Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_url():
    import psycopg

    name = f"fffbt_errcat_test_{id(object())}"
    port = 54398
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD=postgres",
         "-p", f"{port}:5432", "postgres:17-alpine"],
        check=True, capture_output=True,
    )
    dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
    try:
        for _ in range(30):
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            pytest.fail("Postgres container did not become ready")

        with psycopg.connect(dsn) as conn:
            conn.autocommit = True
            for mig in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if "_remote_schema" in mig.name:
                    continue
                conn.execute(mig.read_text())
            conn.execute(SEED_FILE.read_text())
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _make_job(conn, *, video_status="reserved"):
    ids = {k: str(uuid.uuid4()) for k in
           ("acct", "env", "proxy", "dp", "gps", "app", "video", "device", "job")}
    conn.execute("INSERT INTO automation.proxies (id, host, port) VALUES (%s,'127.0.0.1',8080)", (ids["proxy"],))
    conn.execute(
        "INSERT INTO automation.device_profiles (id, brand, model, android_version, screen_width, screen_height, screen_density) "
        "VALUES (%s,'Samsung','S21','12',1080,2400,420)", (ids["dp"],))
    conn.execute("INSERT INTO automation.gps_locations (id, label, latitude, longitude) VALUES (%s,'NYC',40.7,-74.0)", (ids["gps"],))
    conn.execute("INSERT INTO automation.app_states (id) VALUES (%s)", (ids["app"],))
    conn.execute("INSERT INTO automation.accounts (id, username, password, status) VALUES (%s,%s,'p','active')",
                 (ids["acct"], f"u_{ids['acct'][:8]}"))
    conn.execute(
        "INSERT INTO automation.account_environments (id, account_id, proxy_id, device_profile_id, gps_location_id, app_state_id) "
        "VALUES (%s,%s,%s,%s,%s,%s)", (ids["env"], ids["acct"], ids["proxy"], ids["dp"], ids["gps"], ids["app"]))
    conn.execute("INSERT INTO automation.videos (id, source_path, filename, status) VALUES (%s,'p/','f.mp4',%s)",
                 (ids["video"], video_status))
    conn.execute(
        "INSERT INTO automation.physical_devices (id, alias, adb_serial, status, current_job_id, last_seen_at) "
        "VALUES (%s,%s,%s,'busy',NULL,now())", (ids["device"], f"d_{ids['device'][:8]}", f"emu-{ids['device'][:4]}"))
    conn.execute(
        "INSERT INTO automation.jobs (id, video_id, account_id, environment_id, device_id, status) "
        "VALUES (%s,%s,%s,%s,%s,'publishing')",
        (ids["job"], ids["video"], ids["acct"], ids["env"], ids["device"]))
    conn.execute("UPDATE automation.physical_devices SET current_job_id=%s WHERE id=%s", (ids["job"], ids["device"]))
    return ids


@pytest.mark.integration
class TestProcessJobErrorSideEffects:
    def _run(self, dsn, ids, code):
        import psycopg
        with psycopg.connect(dsn) as conn:
            conn.autocommit = True
            conn.execute("SELECT automation.process_job_error(%s::uuid, %s, %s)", (ids["job"], code, "test"))

    def test_account_suspended_sets_account_suspended(self, db_url):
        import psycopg
        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            ids = _make_job(conn)
        self._run(db_url, ids, "account_suspended")
        with psycopg.connect(db_url) as conn:
            acct = conn.execute("SELECT status FROM automation.accounts WHERE id=%s", (ids["acct"],)).fetchone()[0]
            job = conn.execute("SELECT status FROM automation.jobs WHERE id=%s", (ids["job"],)).fetchone()[0]
            assert acct == "suspended"
            assert job == "failed"

    def test_login_challenge_disables_account_no_retry(self, db_url):
        import psycopg
        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            ids = _make_job(conn)
        self._run(db_url, ids, "login_challenge")
        with psycopg.connect(db_url) as conn:
            acct = conn.execute("SELECT status FROM automation.accounts WHERE id=%s", (ids["acct"],)).fetchone()[0]
            job, retry = conn.execute("SELECT status, retry_count FROM automation.jobs WHERE id=%s", (ids["job"],)).fetchone()
            assert acct == "disabled"
            assert job == "failed"   # terminal, not re-queued
            assert retry == 0

    def test_unexpected_destructive_dialog_needs_review(self, db_url):
        import psycopg
        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            ids = _make_job(conn)
        self._run(db_url, ids, "unexpected_destructive_dialog")
        with psycopg.connect(db_url) as conn:
            acct = conn.execute("SELECT status FROM automation.accounts WHERE id=%s", (ids["acct"],)).fetchone()[0]
            job = conn.execute("SELECT status FROM automation.jobs WHERE id=%s", (ids["job"],)).fetchone()[0]
            assert job == "needs_review"
            assert acct == "active"  # not auto-disabled

    def test_device_released_on_terminal(self, db_url):
        import psycopg
        with psycopg.connect(db_url) as conn:
            conn.autocommit = True
            ids = _make_job(conn)
        self._run(db_url, ids, "account_suspended")
        with psycopg.connect(db_url) as conn:
            dev = conn.execute("SELECT status, current_job_id FROM automation.physical_devices WHERE id=%s", (ids["device"],)).fetchone()
            assert dev[0] == "online"
            assert dev[1] is None
