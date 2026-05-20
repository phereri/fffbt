#!/usr/bin/env bash
# Tests for automation.process_job_error() resource cleanup (FFF-52).
#
# Spins up a throwaway Postgres 17 container, applies all migrations + seed,
# inserts test fixtures, then validates:
#   1. Retryable path keeps device busy and video reserved.
#   2. Terminal failed releases device (status=online, current_job_id=NULL).
#   3. Terminal failed moves video to 'new'.
#   4. Terminal needs_review releases device.
#   5. Terminal needs_review sets video to 'needs_review'.
#   6. device_events job_released written on terminal paths.
#   7. job_events error written on terminal paths.
#   8. No device retains current_job_id after terminal error.
#   9. Unknown error code raises exception.
#  10. Retries-exhausted path releases resources.
#
# Requires: docker, psql.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SEED_FILE="$REPO_ROOT/supabase/seed.sql"

CONTAINER="fffbt_process_error_test_$$"
HOST_PORT="${HOST_PORT:-54399}"
export PGPASSWORD=postgres
PSQL=(psql -h 127.0.0.1 -p "$HOST_PORT" -U postgres -d postgres -v ON_ERROR_STOP=1)

PASS=0
FAIL=0

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label (expected='$expected', got='$actual')"
        FAIL=$((FAIL + 1))
    fi
}

echo "[1/4] Starting Postgres 17 container on :$HOST_PORT ..."
docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=postgres \
    -p "$HOST_PORT:5432" \
    postgres:17-alpine >/dev/null
until docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done

echo "[2/4] Applying migrations + seed ..."
for f in $(ls -1 "$MIGRATIONS_DIR"/*.sql | sort); do
    [[ "$(basename "$f")" == *_remote_schema.sql ]] && continue
    "${PSQL[@]}" -f "$f" >/dev/null
done
"${PSQL[@]}" -f "$SEED_FILE" >/dev/null

echo "[3/4] Inserting test fixtures ..."
"${PSQL[@]}" <<'SQL' >/dev/null
CREATE OR REPLACE FUNCTION automation._test_create_env(p_acct uuid, p_env uuid)
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
    v_proxy uuid := gen_random_uuid();
    v_dp    uuid := gen_random_uuid();
    v_gps   uuid := gen_random_uuid();
    v_app   uuid := gen_random_uuid();
BEGIN
    INSERT INTO automation.proxies (id, host, port) VALUES (v_proxy, '127.0.0.1', 8080);
    INSERT INTO automation.device_profiles (id, brand, model, android_version, screen_width, screen_height, screen_density)
        VALUES (v_dp, 'Samsung', 'Galaxy S21', '12', 1080, 2400, 420);
    INSERT INTO automation.gps_locations (id, label, latitude, longitude)
        VALUES (v_gps, 'NYC', 40.7128, -74.0060);
    INSERT INTO automation.app_states (id) VALUES (v_app);
    INSERT INTO automation.account_environments (id, account_id, proxy_id, device_profile_id, gps_location_id, app_state_id)
        VALUES (p_env, p_acct, v_proxy, v_dp, v_gps, v_app);
END;
$fn$;

-- Helper: create a complete job in preparing_device state for testing error paths.
CREATE OR REPLACE FUNCTION automation._test_create_active_job(
    p_vid uuid, p_acct uuid, p_env uuid, p_dev uuid
) RETURNS uuid LANGUAGE plpgsql AS $fn$
DECLARE
    v_job_id uuid;
BEGIN
    INSERT INTO automation.videos (id, source_path, filename, status)
        VALUES (p_vid, '/v/' || p_vid::text || '.mp4', p_vid::text || '.mp4', 'reserved');

    INSERT INTO automation.jobs (id, video_id, account_id, environment_id, device_id, status)
        VALUES (gen_random_uuid(), p_vid, p_acct, p_env, p_dev, 'queued')
        RETURNING id INTO v_job_id;

    UPDATE automation.physical_devices
        SET status = 'busy', current_job_id = v_job_id
        WHERE id = p_dev;

    PERFORM automation.transition_job_status(v_job_id, 'preparing_device', NULL);

    RETURN v_job_id;
END;
$fn$;

DO $$
DECLARE
    id_acct uuid := 'a1000000-0000-0000-0000-000000000001';
    id_env  uuid := 'e1000000-0000-0000-0000-000000000001';
BEGIN
    INSERT INTO automation.accounts (id, username, password, status)
        VALUES (id_acct, 'test_acct', 'pw', 'active');
    PERFORM automation._test_create_env(id_acct, id_env);
END;
$$;
SQL

echo "[4/4] Running process_job_error tests ..."

# ---- Test 1: retryable path keeps device busy and video reserved ----
RESULT=$("${PSQL[@]}" -At <<'SQL'
DO $$
DECLARE
    v_dev uuid := 'dd100000-0000-0000-0000-000000000001';
    v_vid uuid := 'ff100000-0000-0000-0000-000000000001';
    v_job uuid;
BEGIN
    INSERT INTO automation.physical_devices (id, alias, status, last_seen_at)
        VALUES (v_dev, 'dev-retry', 'online', now());
    v_job := automation._test_create_active_job(
        v_vid, 'a1000000-0000-0000-0000-000000000001',
        'e1000000-0000-0000-0000-000000000001', v_dev);
    PERFORM automation.process_job_error(v_job, 'proxy_failed', 'test retry');
END;
$$;
SQL
)
DEV_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status || '|' || COALESCE(current_job_id::text, 'NULL')
    FROM automation.physical_devices WHERE id = 'dd100000-0000-0000-0000-000000000001';
")
VID_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff100000-0000-0000-0000-000000000001';
")
JOB_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.jobs WHERE video_id = 'ff100000-0000-0000-0000-000000000001';
")
# Device should still be busy with the job
if [[ "$DEV_STATUS" == busy* ]]; then
    echo "  PASS: retryable — device stays busy"
    PASS=$((PASS + 1))
else
    echo "  FAIL: retryable — device should stay busy (got='$DEV_STATUS')"
    FAIL=$((FAIL + 1))
fi
assert_eq "retryable — video stays reserved" "reserved" "$VID_STATUS"
assert_eq "retryable — job re-queued" "queued" "$JOB_STATUS"

# Verify retry event was written
RETRY_EVT=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.job_events
    WHERE job_id = (SELECT id FROM automation.jobs WHERE video_id = 'ff100000-0000-0000-0000-000000000001')
      AND event_type = 'retry';
")
assert_eq "retryable — retry event written" "1" "$RETRY_EVT"

# Clean up: cancel re-queued job so account is free for subsequent tests
"${PSQL[@]}" -c "
    UPDATE automation.jobs SET status = 'cancelled', finished_at = now()
    WHERE video_id = 'ff100000-0000-0000-0000-000000000001' AND status = 'queued';
    UPDATE automation.physical_devices SET status = 'online', current_job_id = NULL
    WHERE id = 'dd100000-0000-0000-0000-000000000001';
" >/dev/null

# ---- Test 2+3: terminal failed releases device and moves video to 'new' ----
"${PSQL[@]}" -At <<'SQL' >/dev/null
DO $$
DECLARE
    v_dev uuid := 'dd100000-0000-0000-0000-000000000002';
    v_vid uuid := 'ff100000-0000-0000-0000-000000000002';
    v_job uuid;
BEGIN
    INSERT INTO automation.physical_devices (id, alias, status, last_seen_at)
        VALUES (v_dev, 'dev-fail', 'online', now());
    v_job := automation._test_create_active_job(
        v_vid, 'a1000000-0000-0000-0000-000000000001',
        'e1000000-0000-0000-0000-000000000001', v_dev);
    PERFORM automation.process_job_error(v_job, 'logged_out', 'forced logout');
END;
$$;
SQL

DEV_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status || '|' || COALESCE(current_job_id::text, 'NULL')
    FROM automation.physical_devices WHERE id = 'dd100000-0000-0000-0000-000000000002';
")
assert_eq "terminal failed — device released (online|NULL)" "online|NULL" "$DEV_STATUS"

VID_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff100000-0000-0000-0000-000000000002';
")
assert_eq "terminal failed — video moved to new" "new" "$VID_STATUS"

# ---- Test 4+5: terminal needs_review releases device and sets video needs_review ----
"${PSQL[@]}" -At <<'SQL' >/dev/null
DO $$
DECLARE
    v_dev uuid := 'dd100000-0000-0000-0000-000000000003';
    v_vid uuid := 'ff100000-0000-0000-0000-000000000003';
    v_job uuid;
BEGIN
    INSERT INTO automation.physical_devices (id, alias, status, last_seen_at)
        VALUES (v_dev, 'dev-review', 'online', now());
    v_job := automation._test_create_active_job(
        v_vid, 'a1000000-0000-0000-0000-000000000001',
        'e1000000-0000-0000-0000-000000000001', v_dev);
    PERFORM automation.process_job_error(v_job, 'captcha', 'captcha detected');
END;
$$;
SQL

DEV_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status || '|' || COALESCE(current_job_id::text, 'NULL')
    FROM automation.physical_devices WHERE id = 'dd100000-0000-0000-0000-000000000003';
")
assert_eq "needs_review — device released (online|NULL)" "online|NULL" "$DEV_STATUS"

VID_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff100000-0000-0000-0000-000000000003';
")
assert_eq "needs_review — video set to needs_review" "needs_review" "$VID_STATUS"

# Clean up: cancel needs_review job so account is free for subsequent tests
"${PSQL[@]}" -c "
    UPDATE automation.jobs SET status = 'cancelled', finished_at = now()
    WHERE video_id = 'ff100000-0000-0000-0000-000000000003' AND status = 'needs_review';
" >/dev/null

# ---- Test 6: device_events job_released written for terminal paths ----
RELEASED_EVENTS=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.device_events
    WHERE event_type = 'job_released'
      AND device_id IN (
          'dd100000-0000-0000-0000-000000000002',
          'dd100000-0000-0000-0000-000000000003'
      );
")
assert_eq "device_events job_released written for terminal paths" "2" "$RELEASED_EVENTS"

# No device_events job_released for the retryable device
RETRY_DEV_RELEASED=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.device_events
    WHERE event_type = 'job_released'
      AND device_id = 'dd100000-0000-0000-0000-000000000001';
")
assert_eq "no device_events job_released for retryable path" "0" "$RETRY_DEV_RELEASED"

# ---- Test 7: job_events error written for terminal paths ----
ERROR_EVENTS=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.job_events
    WHERE event_type = 'error'
      AND job_id IN (
          SELECT id FROM automation.jobs
          WHERE video_id IN (
              'ff100000-0000-0000-0000-000000000002',
              'ff100000-0000-0000-0000-000000000003'
          )
      );
")
assert_eq "job_events error written for terminal paths" "2" "$ERROR_EVENTS"

# No error event for retryable path
RETRY_ERROR_EVT=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.job_events
    WHERE event_type = 'error'
      AND job_id = (SELECT id FROM automation.jobs WHERE video_id = 'ff100000-0000-0000-0000-000000000001');
")
assert_eq "no job_events error for retryable path" "0" "$RETRY_ERROR_EVT"

# ---- Test 8: no device retains current_job_id after terminal error ----
STUCK_DEVICES=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.physical_devices pd
    JOIN automation.jobs j ON pd.current_job_id = j.id
    WHERE j.status IN ('failed', 'needs_review');
")
assert_eq "no device retains current_job_id for terminal jobs" "0" "$STUCK_DEVICES"

# ---- Test 9: unknown error code raises exception ----
ERR_OUTPUT=$("${PSQL[@]}" -At -c "
    SELECT automation.process_job_error(
        (SELECT id FROM automation.jobs LIMIT 1),
        'totally_fake_error',
        'should explode'
    );
" 2>&1 || true)
if echo "$ERR_OUTPUT" | grep -q "unknown error code"; then
    echo "  PASS: unknown error code raises exception"
    PASS=$((PASS + 1))
else
    echo "  FAIL: unknown error code did not raise (got='$ERR_OUTPUT')"
    FAIL=$((FAIL + 1))
fi

# ---- Test 10: retries exhausted releases resources ----
"${PSQL[@]}" -At <<'SQL' >/dev/null
DO $$
DECLARE
    v_dev uuid := 'dd100000-0000-0000-0000-000000000004';
    v_vid uuid := 'ff100000-0000-0000-0000-000000000004';
    v_job uuid;
BEGIN
    INSERT INTO automation.physical_devices (id, alias, status, last_seen_at)
        VALUES (v_dev, 'dev-exhaust', 'online', now());
    v_job := automation._test_create_active_job(
        v_vid, 'a1000000-0000-0000-0000-000000000001',
        'e1000000-0000-0000-0000-000000000001', v_dev);
    -- proxy_failed has max_retries=3; exhaust them
    PERFORM automation.process_job_error(v_job, 'proxy_failed', 'retry 1');
    PERFORM automation.transition_job_status(v_job, 'preparing_device', NULL);
    PERFORM automation.process_job_error(v_job, 'proxy_failed', 'retry 2');
    PERFORM automation.transition_job_status(v_job, 'preparing_device', NULL);
    PERFORM automation.process_job_error(v_job, 'proxy_failed', 'retry 3');
    PERFORM automation.transition_job_status(v_job, 'preparing_device', NULL);
    -- 4th error: retries exhausted, should go terminal
    PERFORM automation.process_job_error(v_job, 'proxy_failed', 'exhausted');
END;
$$;
SQL

DEV_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status || '|' || COALESCE(current_job_id::text, 'NULL')
    FROM automation.physical_devices WHERE id = 'dd100000-0000-0000-0000-000000000004';
")
assert_eq "retries exhausted — device released (online|NULL)" "online|NULL" "$DEV_STATUS"

VID_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff100000-0000-0000-0000-000000000004';
")
assert_eq "retries exhausted — video moved to new" "new" "$VID_STATUS"

JOB_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.jobs WHERE video_id = 'ff100000-0000-0000-0000-000000000004';
")
assert_eq "retries exhausted — job is failed" "failed" "$JOB_STATUS"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
echo "PASS: process_job_error tests"
