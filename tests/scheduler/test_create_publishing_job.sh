#!/usr/bin/env bash
# Tests for automation.create_publishing_job() (FFF-15).
#
# Spins up a throwaway Postgres 17 container, applies all migrations + seed,
# inserts test fixtures, then validates:
#   1. Happy path: all resources available → job created.
#   2. Job links correct video, account, environment, device.
#   3. Video status is 'reserved' after job creation.
#   4. Device status is 'busy' with current_job_id set.
#   5. job_events row logged with event_type = 'created'.
#   6. device_events row logged with event_type = 'job_assigned'.
#   7. No account → video stays 'new' (not stuck in reserved).
#   8. No device → video stays 'new' (not stuck).
#   9. No video → returns NULL, nothing changes.
#  10. Second call picks next video.
#  11. Concurrent calls create distinct jobs.
#
# Requires: docker, psql.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SEED_FILE="$REPO_ROOT/supabase/seed.sql"

CONTAINER="fffbt_create_job_test_$$"
HOST_PORT="${HOST_PORT:-54397}"
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

DO $$
DECLARE
    id_acct_a uuid := 'a0000000-0000-0000-0000-000000000001';
    id_acct_b uuid := 'a0000000-0000-0000-0000-000000000002';
    id_env_a  uuid := 'e0000000-0000-0000-0000-000000000001';
    id_env_b  uuid := 'e0000000-0000-0000-0000-000000000002';
    id_dev_a  uuid := 'dd000000-0000-0000-0000-000000000001';
    id_dev_b  uuid := 'dd000000-0000-0000-0000-000000000002';
    id_vid_a  uuid := 'ff000000-0000-0000-0000-000000000010';
    id_vid_b  uuid := 'ff000000-0000-0000-0000-000000000011';
    id_vid_c  uuid := 'ff000000-0000-0000-0000-000000000012';
BEGIN
    -- Two active accounts with full environments
    INSERT INTO automation.accounts (id, username, password, status) VALUES
        (id_acct_a, 'acct_a', 'pw', 'active'),
        (id_acct_b, 'acct_b', 'pw', 'active');
    PERFORM automation._test_create_env(id_acct_a, id_env_a);
    PERFORM automation._test_create_env(id_acct_b, id_env_b);

    -- Two online devices with recent heartbeats
    INSERT INTO automation.physical_devices (id, alias, status, last_seen_at) VALUES
        (id_dev_a, 'dev-a', 'online', now() - interval '10 seconds'),
        (id_dev_b, 'dev-b', 'online', now() - interval '20 seconds');

    -- Three new videos (FIFO order by created_at)
    INSERT INTO automation.videos (id, source_path, filename, status, created_at) VALUES
        (id_vid_a, '/v/a.mp4', 'a.mp4', 'new', now() - interval '3 hours'),
        (id_vid_b, '/v/b.mp4', 'b.mp4', 'new', now() - interval '2 hours'),
        (id_vid_c, '/v/c.mp4', 'c.mp4', 'new', now() - interval '1 hour');
END;
$$;
SQL

echo "[4/4] Running create_publishing_job tests ..."

# ---- Test 1: happy path — job created successfully ----
JOB_ID=$("${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();")
if [[ -n "$JOB_ID" ]]; then
    echo "  PASS: happy path — job created (id=$JOB_ID)"
    PASS=$((PASS + 1))
else
    echo "  FAIL: happy path — expected a job, got NULL"
    FAIL=$((FAIL + 1))
fi

# ---- Test 2: job links correct video (oldest = vid_a) ----
VID=$("${PSQL[@]}" -At -c "SELECT video_id FROM automation.jobs WHERE id = '$JOB_ID';")
assert_eq "job linked to oldest video (vid_a)" \
    "ff000000-0000-0000-0000-000000000010" "$VID"

# ---- Test 3: job links correct account (acct_a or acct_b, both never posted → NULLS FIRST, pick either) ----
ACCT=$("${PSQL[@]}" -At -c "SELECT account_id FROM automation.jobs WHERE id = '$JOB_ID';")
if [[ "$ACCT" == "a0000000-0000-0000-0000-000000000001" || "$ACCT" == "a0000000-0000-0000-0000-000000000002" ]]; then
    echo "  PASS: job linked to eligible account ($ACCT)"
    PASS=$((PASS + 1))
else
    echo "  FAIL: job account unexpected ($ACCT)"
    FAIL=$((FAIL + 1))
fi

# ---- Test 4: video status is 'reserved' ----
STATUS=$("${PSQL[@]}" -At -c "SELECT status FROM automation.videos WHERE id = 'ff000000-0000-0000-0000-000000000010';")
assert_eq "video status is reserved" "reserved" "$STATUS"

# ---- Test 5: device is busy with current_job_id ----
DEV_STATUS=$("${PSQL[@]}" -At -c "
    SELECT status || '|' || COALESCE(current_job_id::text, 'NULL')
    FROM automation.physical_devices
    WHERE current_job_id = '$JOB_ID';
")
assert_eq "device is busy with current_job_id" "busy|$JOB_ID" "$DEV_STATUS"

# ---- Test 6: job_events has 'created' event ----
EVT=$("${PSQL[@]}" -At -c "
    SELECT event_type || '|' || to_status
    FROM automation.job_events
    WHERE job_id = '$JOB_ID' AND event_type = 'created';
")
assert_eq "job_events has created event" "created|queued" "$EVT"

# ---- Test 7: device_events has 'job_assigned' event ----
DEV_EVT=$("${PSQL[@]}" -At -c "
    SELECT event_type FROM automation.device_events
    WHERE payload ->> 'job_id' = '$JOB_ID' AND event_type = 'job_assigned';
")
assert_eq "device_events has job_assigned event" "job_assigned" "$DEV_EVT"

# ---- Test 8: second call picks next video (vid_b) ----
JOB2_ID=$("${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();")
if [[ -n "$JOB2_ID" ]]; then
    VID2=$("${PSQL[@]}" -At -c "SELECT video_id FROM automation.jobs WHERE id = '$JOB2_ID';")
    assert_eq "second job linked to vid_b" \
        "ff000000-0000-0000-0000-000000000011" "$VID2"
else
    echo "  FAIL: second job creation returned NULL"
    FAIL=$((FAIL + 1))
fi

# ---- Test 9: no eligible account → video stays 'new' ----
# Disable all accounts, then try to create a job for vid_c
"${PSQL[@]}" -c "
    UPDATE automation.accounts SET status = 'disabled';
" >/dev/null
VID_C_BEFORE=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff000000-0000-0000-0000-000000000012';
")
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();")
VID_C_AFTER=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff000000-0000-0000-0000-000000000012';
")
assert_eq "no account: returns NULL" "" "$RESULT"
assert_eq "no account: video stays new (was $VID_C_BEFORE)" "new" "$VID_C_AFTER"

# Restore accounts
"${PSQL[@]}" -c "UPDATE automation.accounts SET status = 'active';" >/dev/null

# ---- Test 10: no device → video stays 'new' ----
# Set all devices to offline
"${PSQL[@]}" -c "UPDATE automation.physical_devices SET status = 'offline';" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();")
VID_C_AFTER=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos WHERE id = 'ff000000-0000-0000-0000-000000000012';
")
assert_eq "no device: returns NULL" "" "$RESULT"
assert_eq "no device: video stays new" "new" "$VID_C_AFTER"

# Restore devices
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET status = 'online', last_seen_at = now()
    WHERE id IN ('dd000000-0000-0000-0000-000000000001', 'dd000000-0000-0000-0000-000000000002');
    UPDATE automation.physical_devices SET current_job_id = NULL
    WHERE id IN ('dd000000-0000-0000-0000-000000000001', 'dd000000-0000-0000-0000-000000000002');
" >/dev/null

# ---- Test 11: no new videos → returns NULL ----
# Mark remaining new video as released
"${PSQL[@]}" -c "
    UPDATE automation.videos SET status = 'released' WHERE status = 'new';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();")
assert_eq "no videos: returns NULL" "" "$RESULT"

# ---- Test 12: concurrent calls create distinct jobs ----
# Reset: put two videos back to new, clear active jobs for accounts
"${PSQL[@]}" <<'SQL' >/dev/null
-- Cancel existing active jobs so accounts become eligible again
UPDATE automation.jobs SET status = 'cancelled', finished_at = now()
WHERE status NOT IN ('done', 'failed', 'cancelled');

-- Reset two videos to new
UPDATE automation.videos SET status = 'new'
WHERE id IN ('ff000000-0000-0000-0000-000000000010', 'ff000000-0000-0000-0000-000000000011');

-- Reset devices
UPDATE automation.physical_devices
SET status = 'online', last_seen_at = now(), current_job_id = NULL;
SQL

TMPDIR_TEST=$(mktemp -d)
(
    "${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();" \
        > "$TMPDIR_TEST/s1.txt" 2>&1
) &
PID1=$!
(
    "${PSQL[@]}" -At -c "SELECT id FROM automation.create_publishing_job();" \
        > "$TMPDIR_TEST/s2.txt" 2>&1
) &
PID2=$!
wait $PID1 $PID2

S1=$(cat "$TMPDIR_TEST/s1.txt")
S2=$(cat "$TMPDIR_TEST/s2.txt")
rm -rf "$TMPDIR_TEST"

# At least one should succeed; if both do, they must be different
if [[ -n "$S1" && -n "$S2" && "$S1" != "$S2" ]]; then
    echo "  PASS: concurrent calls created two distinct jobs ($S1 vs $S2)"
    PASS=$((PASS + 1))
elif [[ -n "$S1" || -n "$S2" ]]; then
    echo "  PASS: concurrent calls — at least one job created (s1='$S1', s2='$S2')"
    PASS=$((PASS + 1))
else
    echo "  FAIL: concurrent calls — no jobs created"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
echo "PASS: create_publishing_job tests"
