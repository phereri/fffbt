#!/usr/bin/env bash
# End-to-end smoke test for scripts/discover_physical_devices.py.
#
# Boots a throwaway Postgres 17 container, applies all migrations (skipping
# remote_schema, which depends on Supabase-only roles), recreates a minimal
# public.device_heartbeats table, seeds physical_devices + heartbeats, then
# runs the discovery script and verifies status transitions and that a
# device_events row was recorded.
#
# Requires: docker, psql, python3 with psycopg installed. If PYTHON_BIN is
# set, uses that interpreter (useful for venv runs).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SCRIPT="$REPO_ROOT/scripts/discover_physical_devices.py"
PYTHON_BIN="${PYTHON_BIN:-python3}"

CONTAINER="fffbt_discovery_test_$$"
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

echo "[1/5] Starting Postgres 17 container on :$HOST_PORT ..."
docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=postgres \
    -p "$HOST_PORT:5432" \
    postgres:17-alpine >/dev/null
until docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done

echo "[2/5] Applying automation migrations ..."
for f in $(ls -1 "$MIGRATIONS_DIR"/*.sql | sort); do
    [[ "$(basename "$f")" == *_remote_schema.sql ]] && continue
    "${PSQL[@]}" -f "$f" >/dev/null
done

echo "[3/5] Creating minimal public.device_heartbeats ..."
"${PSQL[@]}" <<'SQL' >/dev/null
CREATE TABLE public.device_heartbeats (
    host_id text NOT NULL,
    serial  text NOT NULL,
    seen_at timestamptz NOT NULL DEFAULT now(),
    connection_type text NOT NULL,
    state   text NOT NULL,
    ip      text,
    model   text,
    product text,
    device  text,
    transport_id int,
    PRIMARY KEY (host_id, serial)
);
SQL

echo "[4/5] Seeding physical_devices and heartbeats ..."
"${PSQL[@]}" <<'SQL' >/dev/null
-- dev_a: offline, expects to flip online via fresh heartbeat by IP
INSERT INTO automation.physical_devices
    (id, alias, tailscale_ipv4, adb_connect_target, status, last_seen_at)
VALUES
    ('00000000-0000-0000-0000-00000000000a', 'sm-a505f',
     '100.0.0.1', '100.0.0.1:5555', 'offline', NULL);

-- dev_b: online with stale last_seen_at, no fresh heartbeat → should flip offline
INSERT INTO automation.physical_devices
    (id, alias, tailscale_ipv4, adb_connect_target, status, last_seen_at)
VALUES
    ('00000000-0000-0000-0000-00000000000b', 'sm-g970f',
     '100.0.0.2', '100.0.0.2:5555', 'online', now() - interval '1 hour');

-- dev_c: busy, must NOT be touched even though no heartbeat exists.
-- (current_job_id stays NULL; the FK to automation.jobs only constrains the
-- referenced row when set, and the busy status itself has no NOT NULL job
-- requirement in the schema.)
INSERT INTO automation.physical_devices
    (id, alias, tailscale_ipv4, adb_connect_target, status)
VALUES
    ('00000000-0000-0000-0000-00000000000c', 'sm-n950f',
     '100.0.0.3', '100.0.0.3:5555', 'busy');

-- Fresh heartbeat for dev_a with a USB-form serial. Backfill expected.
INSERT INTO public.device_heartbeats
    (host_id, serial, seen_at, connection_type, state, ip)
VALUES
    ('host-1', 'HW_A_001', now() - interval '5 seconds', 'tcpip', 'device', '100.0.0.1');
SQL

echo "[5/5] Running discovery script via --source heartbeat --dry-run first ..."
SUPABASE_DB_URL="postgresql://postgres:postgres@127.0.0.1:$HOST_PORT/postgres" \
    "$PYTHON_BIN" "$SCRIPT" --source heartbeat --dry-run >/tmp/discovery-dry.log

# Dry run should not change anything
DRY_STATUS_A=$("${PSQL[@]}" -At -c \
    "SELECT status FROM automation.physical_devices WHERE id='00000000-0000-0000-0000-00000000000a';")
assert_eq "dry-run leaves dev_a status unchanged" "offline" "$DRY_STATUS_A"

echo "      Running discovery script for real ..."
SUPABASE_DB_URL="postgresql://postgres:postgres@127.0.0.1:$HOST_PORT/postgres" \
    "$PYTHON_BIN" "$SCRIPT" --source heartbeat >/tmp/discovery-real.log

# dev_a: offline → online, adb_serial backfilled
STATUS_A=$("${PSQL[@]}" -At -c \
    "SELECT status FROM automation.physical_devices WHERE id='00000000-0000-0000-0000-00000000000a';")
assert_eq "dev_a flipped to online" "online" "$STATUS_A"

SERIAL_A=$("${PSQL[@]}" -At -c \
    "SELECT adb_serial FROM automation.physical_devices WHERE id='00000000-0000-0000-0000-00000000000a';")
assert_eq "dev_a adb_serial backfilled from heartbeat" "HW_A_001" "$SERIAL_A"

EVENT_A=$("${PSQL[@]}" -At -c \
    "SELECT event_type FROM automation.device_events
     WHERE device_id='00000000-0000-0000-0000-00000000000a' ORDER BY created_at DESC LIMIT 1;")
assert_eq "dev_a recorded 'connected' event" "connected" "$EVENT_A"

# dev_b: online → offline (no heartbeat)
STATUS_B=$("${PSQL[@]}" -At -c \
    "SELECT status FROM automation.physical_devices WHERE id='00000000-0000-0000-0000-00000000000b';")
assert_eq "dev_b flipped to offline" "offline" "$STATUS_B"

EVENT_B=$("${PSQL[@]}" -At -c \
    "SELECT event_type FROM automation.device_events
     WHERE device_id='00000000-0000-0000-0000-00000000000b' ORDER BY created_at DESC LIMIT 1;")
assert_eq "dev_b recorded 'disconnected' event" "disconnected" "$EVENT_B"

# dev_c: busy must remain untouched
STATUS_C=$("${PSQL[@]}" -At -c \
    "SELECT status FROM automation.physical_devices WHERE id='00000000-0000-0000-0000-00000000000c';")
assert_eq "dev_c busy row untouched" "busy" "$STATUS_C"

EVENT_C_COUNT=$("${PSQL[@]}" -At -c \
    "SELECT count(*) FROM automation.device_events
     WHERE device_id='00000000-0000-0000-0000-00000000000c';")
assert_eq "dev_c emitted no events" "0" "$EVENT_C_COUNT"

# Idempotency: second run should not emit duplicate connect/disconnect events
SUPABASE_DB_URL="postgresql://postgres:postgres@127.0.0.1:$HOST_PORT/postgres" \
    "$PYTHON_BIN" "$SCRIPT" --source heartbeat >/tmp/discovery-rerun.log

EVENT_A_COUNT=$("${PSQL[@]}" -At -c \
    "SELECT count(*) FROM automation.device_events
     WHERE device_id='00000000-0000-0000-0000-00000000000a';")
assert_eq "dev_a no duplicate events on rerun" "1" "$EVENT_A_COUNT"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    echo "logs:"
    echo "--- dry run ---"; cat /tmp/discovery-dry.log
    echo "--- real run ---"; cat /tmp/discovery-real.log
    echo "--- rerun ---"; cat /tmp/discovery-rerun.log
    exit 1
fi
echo "PASS: device discovery e2e tests"
