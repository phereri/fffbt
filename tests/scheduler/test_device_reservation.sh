#!/usr/bin/env bash
# Tests for automation.reserve_physical_device() (FFF-14).
#
# Spins up a throwaway Postgres 17 container, applies all migrations + seed,
# inserts test fixtures, then validates:
#   1. Device with recent heartbeat is reserved.
#   2. Reserved device status becomes 'busy'.
#   3. Stale-heartbeat device is skipped.
#   4. Offline device is skipped.
#   5. Busy device is skipped.
#   6. Maintenance device is skipped.
#   7. Second call picks a different device.
#   8. No eligible devices returns NULL.
#   9. Custom heartbeat timeout is respected.
#
# Requires: docker, psql.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SEED_FILE="$REPO_ROOT/supabase/seed.sql"

CONTAINER="fffbt_device_reservation_test_$$"
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
DO $$
DECLARE
    id_dev_a uuid := 'dd000000-0000-0000-0000-000000000001';  -- online, recent heartbeat
    id_dev_b uuid := 'dd000000-0000-0000-0000-000000000002';  -- online, stale heartbeat
    id_dev_c uuid := 'dd000000-0000-0000-0000-000000000003';  -- offline
    id_dev_d uuid := 'dd000000-0000-0000-0000-000000000004';  -- busy
    id_dev_e uuid := 'dd000000-0000-0000-0000-000000000005';  -- maintenance
    id_dev_f uuid := 'dd000000-0000-0000-0000-000000000006';  -- online, recent (second eligible)
    id_dev_g uuid := 'dd000000-0000-0000-0000-000000000007';  -- online, NULL last_seen_at
BEGIN
    INSERT INTO automation.physical_devices (id, alias, status, last_seen_at) VALUES
        (id_dev_a, 'dev-a-online-recent',  'online',      now() - interval '30 seconds'),
        (id_dev_b, 'dev-b-online-stale',   'online',      now() - interval '10 minutes'),
        (id_dev_c, 'dev-c-offline',        'offline',     now() - interval '1 minute'),
        (id_dev_d, 'dev-d-busy',           'busy',        now() - interval '10 seconds'),
        (id_dev_e, 'dev-e-maintenance',    'maintenance', now() - interval '1 minute'),
        (id_dev_f, 'dev-f-online-recent2', 'online',      now() - interval '2 minutes'),
        (id_dev_g, 'dev-g-online-nullhb',  'online',      NULL);
END;
$$;
SQL

echo "[4/4] Running device reservation tests ..."

# ---- Test 1: most-recently-seen online device is reserved ----
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device();")
assert_eq "most-recently-seen online device reserved (dev-a)" \
    "dd000000-0000-0000-0000-000000000001" "$RESULT"

# ---- Test 2: reserved device status is now busy ----
STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.physical_devices
    WHERE id = 'dd000000-0000-0000-0000-000000000001';
")
assert_eq "reserved device status is busy" "busy" "$STATUS"

# ---- Test 3: second call picks different device (dev-f) ----
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device();")
assert_eq "second call picks next eligible device (dev-f)" \
    "dd000000-0000-0000-0000-000000000006" "$RESULT"

# ---- Test 4: stale heartbeat device skipped (dev-b has 10min old heartbeat, default timeout 300s) ----
# dev-a and dev-f are now busy; dev-b has stale heartbeat; dev-g has NULL heartbeat
# No eligible devices should remain with default timeout
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device();")
assert_eq "stale heartbeat and NULL heartbeat devices skipped, returns NULL" "" "$RESULT"

# ---- Test 5: custom timeout includes stale device ----
# Reset dev-f back to online for later tests
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET status = 'online'
    WHERE id = 'dd000000-0000-0000-0000-000000000006';
    UPDATE automation.physical_devices SET status = 'online'
    WHERE id = 'dd000000-0000-0000-0000-000000000001';
" >/dev/null
# dev-b has 10-minute old heartbeat; with 700s timeout it becomes eligible
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device(p_heartbeat_timeout_seconds := 700);")
# dev-a (30s ago) is most recent, should be picked first
assert_eq "custom timeout: most recent device picked" \
    "dd000000-0000-0000-0000-000000000001" "$RESULT"

# ---- Test 6: with tight timeout, only very-recent devices qualify ----
# Reset all online
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET status = 'online'
    WHERE id IN (
        'dd000000-0000-0000-0000-000000000001',
        'dd000000-0000-0000-0000-000000000006'
    );
" >/dev/null
# 10s timeout: only dev-a (30s ago) might just barely fail, dev-f (2min ago) definitely fails
# Let's set dev-a heartbeat to 5s ago for this test
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET last_seen_at = now() - interval '5 seconds'
    WHERE id = 'dd000000-0000-0000-0000-000000000001';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device(p_heartbeat_timeout_seconds := 10);")
assert_eq "tight timeout: only freshest device qualifies (dev-a)" \
    "dd000000-0000-0000-0000-000000000001" "$RESULT"

# ---- Test 7: offline/busy/maintenance devices never selected ----
# Reset everything, disable all online devices, verify others aren't picked
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET status = 'online'
    WHERE id = 'dd000000-0000-0000-0000-000000000001';
    UPDATE automation.physical_devices SET status = 'maintenance'
    WHERE id IN (
        'dd000000-0000-0000-0000-000000000001',
        'dd000000-0000-0000-0000-000000000006'
    );
" >/dev/null
# Now only dev-b (stale), dev-c (offline), dev-d (busy), dev-e (maintenance),
# dev-g (null hb) are left, plus dev-a and dev-f (maintenance)
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device(p_heartbeat_timeout_seconds := 700);")
# dev-b is online with 10min heartbeat, 700s timeout should include it
assert_eq "only online+heartbeat devices eligible (dev-b with large timeout)" \
    "dd000000-0000-0000-0000-000000000002" "$RESULT"

# ---- Test 8: NULL last_seen_at device is skipped ----
# Reset dev-b, make it busy so dev-g (online, NULL heartbeat) is only online candidate
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET status = 'online', last_seen_at = NULL
    WHERE id = 'dd000000-0000-0000-0000-000000000001';
    UPDATE automation.physical_devices SET status = 'online', last_seen_at = NULL
    WHERE id = 'dd000000-0000-0000-0000-000000000006';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device(p_heartbeat_timeout_seconds := 99999);")
# dev-b was reserved in test 7, only dev-a, dev-f, dev-g are online but all have NULL last_seen_at
assert_eq "NULL last_seen_at devices are skipped" "" "$RESULT"

# ---- Test 9: no eligible devices returns NULL ----
"${PSQL[@]}" -c "
    UPDATE automation.physical_devices SET status = 'offline';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT automation.reserve_physical_device();")
assert_eq "all offline returns NULL" "" "$RESULT"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
echo "PASS: device reservation tests"
