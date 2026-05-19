#!/usr/bin/env bash
# Migration smoke test for FFF-41.
#
# Brings up a throwaway Postgres 17 container, applies every file in
# supabase/migrations/ in name order, runs supabase/seed.sql, then verifies:
#   1. All expected automation.* tables exist.
#   2. The fffbt schema (pre-seeded with a sentinel) is left untouched.
#   3. global_settings is populated with the expected seed keys.
#   4. seed.sql is idempotent (running it twice does not change row count).
#
# Requires: docker, psql.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SEED_FILE="$REPO_ROOT/supabase/seed.sql"

CONTAINER="fffbt_migration_smoke_$$"
HOST_PORT="${HOST_PORT:-54399}"
export PGPASSWORD=postgres
PSQL=(psql -h 127.0.0.1 -p "$HOST_PORT" -U postgres -d postgres -v ON_ERROR_STOP=1)

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[1/6] Starting fresh Postgres 17 container on :$HOST_PORT ..."
docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=postgres \
    -p "$HOST_PORT:5432" \
    postgres:17-alpine >/dev/null
until docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done

echo "[2/6] Pre-creating fffbt schema with sentinel data ..."
"${PSQL[@]}" <<'SQL' >/dev/null
CREATE SCHEMA fffbt;
CREATE TABLE fffbt.sentinel (id int PRIMARY KEY, marker text NOT NULL);
INSERT INTO fffbt.sentinel VALUES (1, 'do-not-touch');
SQL

echo "[3/6] Applying migrations ..."
for f in $(ls -1 "$MIGRATIONS_DIR"/*.sql | sort); do
    echo "       $(basename "$f")"
    "${PSQL[@]}" -f "$f" >/dev/null
done

echo "[4/6] Applying seed.sql ..."
"${PSQL[@]}" -f "$SEED_FILE" >/dev/null

echo "[5/6] Verifying schema state ..."

EXPECTED_TABLES=(
    account_environments accounts app_states artifacts
    device_events device_profiles global_settings gps_locations
    job_events job_logs jobs physical_devices proxies videos
)
ACTUAL_TABLES=$("${PSQL[@]}" -At -c \
    "SELECT table_name FROM information_schema.tables \
     WHERE table_schema='automation' ORDER BY table_name;")
for t in "${EXPECTED_TABLES[@]}"; do
    if ! grep -qx "$t" <<<"$ACTUAL_TABLES"; then
        echo "FAIL: missing table automation.$t"
        echo "Tables found:"; echo "$ACTUAL_TABLES"
        exit 1
    fi
done
echo "       ok: all 14 automation.* tables present"

SENTINEL_MARKER=$("${PSQL[@]}" -At -c "SELECT marker FROM fffbt.sentinel WHERE id=1;")
if [[ "$SENTINEL_MARKER" != "do-not-touch" ]]; then
    echo "FAIL: fffbt.sentinel was modified (marker='$SENTINEL_MARKER')"
    exit 1
fi
FFFBT_TABLES=$("${PSQL[@]}" -At -c \
    "SELECT table_name FROM information_schema.tables \
     WHERE table_schema='fffbt' ORDER BY table_name;")
if [[ "$FFFBT_TABLES" != "sentinel" ]]; then
    echo "FAIL: fffbt schema has unexpected tables: $FFFBT_TABLES"
    exit 1
fi
echo "       ok: fffbt schema untouched"

EXPECTED_KEYS=(
    daily_posts_limit_per_account job_heartbeat_timeout_seconds
    max_parallel_jobs post_interval_max_seconds
    post_interval_min_seconds verification_delay_seconds
)
ACTUAL_KEYS=$("${PSQL[@]}" -At -c \
    "SELECT key FROM automation.global_settings ORDER BY key;")
for k in "${EXPECTED_KEYS[@]}"; do
    if ! grep -qx "$k" <<<"$ACTUAL_KEYS"; then
        echo "FAIL: missing seed key $k"
        exit 1
    fi
done
echo "       ok: 6 seed keys present in automation.global_settings"

echo "[6/6] Verifying seed idempotency ..."
"${PSQL[@]}" -f "$SEED_FILE" >/dev/null
COUNT=$("${PSQL[@]}" -At -c "SELECT count(*) FROM automation.global_settings;")
if [[ "$COUNT" != "6" ]]; then
    echo "FAIL: seed not idempotent (row count=$COUNT)"
    exit 1
fi
echo "       ok: seed re-run leaves 6 rows"

echo ""
echo "PASS: migration smoke test"
