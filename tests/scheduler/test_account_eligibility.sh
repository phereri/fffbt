#!/usr/bin/env bash
# Tests for automation.find_eligible_account() (FFF-12).
#
# Spins up a throwaway Postgres 17 container, applies all migrations + seed,
# inserts test fixtures, then validates:
#   1. Account with oldest last_published_at is selected.
#   2. Account that never posted (NULL) is selected first.
#   3. Account exceeding daily limit is skipped.
#   4. Account in cooldown is skipped.
#   5. Account with active job is skipped.
#   6. Account without environment is skipped.
#   7. Disabled/banned account is skipped.
#
# Requires: docker, psql.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SEED_FILE="$REPO_ROOT/supabase/seed.sql"

CONTAINER="fffbt_eligibility_test_$$"
HOST_PORT="${HOST_PORT:-54398}"
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
    # Skip remote_schema (legacy fffbt schema requires Supabase roles/extensions)
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
SQL

"${PSQL[@]}" -At <<'SQL' >/dev/null
-- Helper: deterministic UUIDs for readability.
-- acct_a: active, posted 2h ago   (eligible, oldest)
-- acct_b: active, posted 30min ago (in cooldown)
-- acct_c: active, never posted     (eligible, NULL → first)
-- acct_d: active, has active job   (ineligible)
-- acct_e: disabled                 (ineligible)
-- acct_f: active, no environment   (ineligible)
-- acct_g: active, hit daily limit  (ineligible)

DO $$
DECLARE
    id_acct_a uuid := 'a0000000-0000-0000-0000-000000000001';
    id_acct_b uuid := 'a0000000-0000-0000-0000-000000000002';
    id_acct_c uuid := 'a0000000-0000-0000-0000-000000000003';
    id_acct_d uuid := 'a0000000-0000-0000-0000-000000000004';
    id_acct_e uuid := 'a0000000-0000-0000-0000-000000000005';
    id_acct_f uuid := 'a0000000-0000-0000-0000-000000000006';
    id_acct_g uuid := 'a0000000-0000-0000-0000-000000000007';

    id_env_a  uuid := 'e0000000-0000-0000-0000-000000000001';
    id_env_b  uuid := 'e0000000-0000-0000-0000-000000000002';
    id_env_c  uuid := 'e0000000-0000-0000-0000-000000000003';
    id_env_d  uuid := 'e0000000-0000-0000-0000-000000000004';
    id_env_e  uuid := 'e0000000-0000-0000-0000-000000000005';
    id_env_g  uuid := 'e0000000-0000-0000-0000-000000000007';

    id_device uuid := 'dd000000-0000-0000-0000-000000000001';
    id_video  uuid := 'ff000000-0000-0000-0000-000000000001';
    id_vid2   uuid := 'ff000000-0000-0000-0000-000000000002';
    id_vid_d  uuid := 'ff000000-0000-0000-0000-000000000003';

    i int;
    vid_id uuid;
BEGIN
    -- Create accounts
    INSERT INTO automation.accounts (id, username, password, status) VALUES
        (id_acct_a, 'acct_a', 'pw', 'active'),
        (id_acct_b, 'acct_b', 'pw', 'active'),
        (id_acct_c, 'acct_c', 'pw', 'active'),
        (id_acct_d, 'acct_d', 'pw', 'active'),
        (id_acct_e, 'acct_e', 'pw', 'disabled'),
        (id_acct_f, 'acct_f', 'pw', 'active'),
        (id_acct_g, 'acct_g', 'pw', 'active');

    -- Create environment components and link them for each account (except acct_f).
    -- Helper: inline per-account environment creation.
    PERFORM automation._test_create_env(id_acct_a, id_env_a);
    PERFORM automation._test_create_env(id_acct_b, id_env_b);
    PERFORM automation._test_create_env(id_acct_c, id_env_c);
    PERFORM automation._test_create_env(id_acct_d, id_env_d);
    PERFORM automation._test_create_env(id_acct_e, id_env_e);
    PERFORM automation._test_create_env(id_acct_g, id_env_g);

    -- Physical device (shared across test jobs)
    INSERT INTO automation.physical_devices (id, alias, status)
        VALUES (id_device, 'test-device', 'online');

    -- Videos
    INSERT INTO automation.videos (id, source_path, filename, status) VALUES
        (id_video, '/v/1.mp4', '1.mp4', 'new'),
        (id_vid2,  '/v/2.mp4', '2.mp4', 'new'),
        (id_vid_d, '/v/d.mp4', 'd.mp4', 'reserved');

    -- acct_a: one completed job 2 hours ago
    INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status, started_at, finished_at)
        VALUES (id_video, id_acct_a, id_env_a, id_device, 'done',
                now() - interval '3 hours', now() - interval '2 hours');

    -- acct_b: one completed job 30 min ago (within default cooldown of 900s = 15min)
    INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status, started_at, finished_at)
        VALUES (id_vid2, id_acct_b, id_env_b, id_device, 'done',
                now() - interval '1 hour', now() - interval '30 minutes');

    -- acct_d: one active (queued) job
    INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status)
        VALUES (id_vid_d, id_acct_d, id_env_d, id_device, 'queued');

    -- acct_g: 20 completed jobs in last 24h (hits daily limit of 20)
    FOR i IN 1..20 LOOP
        vid_id := gen_random_uuid();
        INSERT INTO automation.videos (id, source_path, filename, status)
            VALUES (vid_id, '/v/g' || i || '.mp4', 'g' || i || '.mp4', 'released');
        INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status, started_at, finished_at)
            VALUES (vid_id, id_acct_g, id_env_g, id_device, 'done',
                    now() - interval '20 hours', now() - interval '1 hour');
    END LOOP;
END;
$$;
SQL

echo "[4/4] Running eligibility tests ..."

# ---- Test 1: NULL last_published_at wins (acct_c never posted) ----
RESULT=$("${PSQL[@]}" -At -c "SELECT account_id FROM automation.find_eligible_account();")
assert_eq "never-posted account selected first" \
    "a0000000-0000-0000-0000-000000000003" "$RESULT"

# ---- Test 2: after removing acct_c's environment, oldest poster (acct_a) is selected ----
"${PSQL[@]}" -At -c "
    DELETE FROM automation.account_environments
    WHERE account_id = 'a0000000-0000-0000-0000-000000000003';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT account_id FROM automation.find_eligible_account();")
assert_eq "oldest last_published_at selected (acct_a, 2h ago)" \
    "a0000000-0000-0000-0000-000000000001" "$RESULT"

# ---- Test 3: acct_b is in cooldown (30min < 15min default → actually 30min > 15min, so eligible) ----
# With default cooldown (900s = 15min), acct_b posted 30min ago → eligible.
# With cooldown = 3600 (1h), acct_b posted 30min ago → ineligible.
RESULT=$("${PSQL[@]}" -At -c "SELECT account_id FROM automation.find_eligible_account(p_cooldown_seconds := 3600);")
assert_eq "cooldown=3600s skips acct_b (posted 30min ago), picks acct_a" \
    "a0000000-0000-0000-0000-000000000001" "$RESULT"

# ---- Test 4: acct_d (active job) is never selected ----
# Disable acct_a so acct_d would be next by ordering if eligible
"${PSQL[@]}" -c "
    UPDATE automation.accounts SET status = 'disabled'
    WHERE id = 'a0000000-0000-0000-0000-000000000001';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT account_id FROM automation.find_eligible_account();")
# acct_b (30min ago, past default 900s cooldown) should be picked; acct_d skipped
assert_eq "active-job account (acct_d) skipped, acct_b selected" \
    "a0000000-0000-0000-0000-000000000002" "$RESULT"
# Restore acct_a
"${PSQL[@]}" -c "
    UPDATE automation.accounts SET status = 'active'
    WHERE id = 'a0000000-0000-0000-0000-000000000001';
" >/dev/null

# ---- Test 5: acct_e (disabled) is never selected ----
RESULT=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.find_eligible_account()
    WHERE account_id = 'a0000000-0000-0000-0000-000000000005';
")
assert_eq "disabled account never selected" "0" "$RESULT"

# ---- Test 6: acct_f (no environment) is never selected ----
RESULT=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.find_eligible_account()
    WHERE account_id = 'a0000000-0000-0000-0000-000000000006';
")
assert_eq "no-environment account never selected" "0" "$RESULT"

# ---- Test 7: acct_g (daily limit hit) is never selected ----
RESULT=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.find_eligible_account()
    WHERE account_id = 'a0000000-0000-0000-0000-000000000007';
")
assert_eq "daily-limit account never selected" "0" "$RESULT"

# ---- Test 8: daily limit boundary — 19 posts is OK, 20 is not ----
# Delete one of acct_g's done jobs to bring count to 19
"${PSQL[@]}" -c "
    DELETE FROM automation.jobs
    WHERE id = (
        SELECT id FROM automation.jobs
        WHERE account_id = 'a0000000-0000-0000-0000-000000000007' AND status = 'done'
        LIMIT 1
    );
" >/dev/null
# Disable other eligible accounts so acct_g is the only candidate
"${PSQL[@]}" -c "
    UPDATE automation.accounts SET status = 'disabled'
    WHERE id IN (
        'a0000000-0000-0000-0000-000000000001',
        'a0000000-0000-0000-0000-000000000002'
    );
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT account_id FROM automation.find_eligible_account();")
assert_eq "daily limit boundary: 19 posts → eligible" \
    "a0000000-0000-0000-0000-000000000007" "$RESULT"

# ---- Test 9: no eligible accounts → empty result ----
"${PSQL[@]}" -c "
    UPDATE automation.accounts SET status = 'disabled'
    WHERE id = 'a0000000-0000-0000-0000-000000000007';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT count(*) FROM automation.find_eligible_account();")
assert_eq "no eligible accounts returns empty" "0" "$RESULT"

# ---- Test 10: validation/placeholder accounts are excluded from the
#               generic eligibility query, even if otherwise eligible.
# Insert a fresh active account with is_validation = true and a complete
# environment. Without the validation guard, it would be the only eligible
# account; with the guard, the query returns nothing.
"${PSQL[@]}" -At <<'SQL' >/dev/null
DO $$
DECLARE
    id_validation uuid := 'a0000000-0000-0000-0000-0000000000aa';
    id_env_v      uuid := 'e0000000-0000-0000-0000-0000000000aa';
BEGIN
    INSERT INTO automation.accounts (id, username, password, status, is_validation)
        VALUES (id_validation, 'validation_only_acct', 'pw', 'active', true);
    PERFORM automation._test_create_env(id_validation, id_env_v);
END;
$$;
SQL
RESULT=$("${PSQL[@]}" -At -c "SELECT count(*) FROM automation.find_eligible_account();")
assert_eq "validation-only accounts excluded from generic eligibility" "0" "$RESULT"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
echo "PASS: account eligibility tests"
