#!/usr/bin/env bash
# Tests for automation.reserve_next_video() (FFF-13).
#
# Spins up a throwaway Postgres 17 container, applies all migrations + seed,
# inserts test fixtures, then validates:
#   1. Only videos with status = 'new' are reserved.
#   2. Exactly one video is reserved per call.
#   3. Video status is set to 'reserved' after reservation.
#   4. Concurrent callers never reserve the same video.
#   5. Returns NULL when no new videos exist.
#   6. FIFO ordering by created_at.
#
# Requires: docker, psql.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"
SEED_FILE="$REPO_ROOT/supabase/seed.sql"

CONTAINER="fffbt_video_reservation_test_$$"
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
    id_vid_a uuid := 'ff000000-0000-0000-0000-000000000010';
    id_vid_b uuid := 'ff000000-0000-0000-0000-000000000011';
    id_vid_c uuid := 'ff000000-0000-0000-0000-000000000012';
    id_vid_reserved uuid := 'ff000000-0000-0000-0000-000000000013';
    id_vid_failed   uuid := 'ff000000-0000-0000-0000-000000000014';
BEGIN
    -- vid_a: new, oldest (created 3h ago)
    INSERT INTO automation.videos (id, source_path, filename, status, created_at)
        VALUES (id_vid_a, '/v/a.mp4', 'a.mp4', 'new', now() - interval '3 hours');

    -- vid_b: new, middle (created 2h ago)
    INSERT INTO automation.videos (id, source_path, filename, status, created_at)
        VALUES (id_vid_b, '/v/b.mp4', 'b.mp4', 'new', now() - interval '2 hours');

    -- vid_c: new, newest (created 1h ago)
    INSERT INTO automation.videos (id, source_path, filename, status, created_at)
        VALUES (id_vid_c, '/v/c.mp4', 'c.mp4', 'new', now() - interval '1 hour');

    -- vid_reserved: already reserved (should be skipped)
    INSERT INTO automation.videos (id, source_path, filename, status)
        VALUES (id_vid_reserved, '/v/r.mp4', 'r.mp4', 'reserved');

    -- vid_failed: already failed (should be skipped)
    INSERT INTO automation.videos (id, source_path, filename, status)
        VALUES (id_vid_failed, '/v/f.mp4', 'f.mp4', 'failed');
END;
$$;
SQL

echo "[4/4] Running video reservation tests ..."

# ---- Test 1: oldest new video is reserved first (FIFO) ----
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.reserve_next_video();")
assert_eq "oldest new video reserved first (vid_a)" \
    "ff000000-0000-0000-0000-000000000010" "$RESULT"

# ---- Test 2: reserved video has status = 'reserved' in DB ----
STATUS=$("${PSQL[@]}" -At -c "
    SELECT status FROM automation.videos
    WHERE id = 'ff000000-0000-0000-0000-000000000010';
")
assert_eq "vid_a status is 'reserved' after reservation" "reserved" "$STATUS"

# ---- Test 3: next call picks the second-oldest video ----
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.reserve_next_video();")
assert_eq "second call reserves vid_b" \
    "ff000000-0000-0000-0000-000000000011" "$RESULT"

# ---- Test 4: third call picks the last new video ----
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.reserve_next_video();")
assert_eq "third call reserves vid_c" \
    "ff000000-0000-0000-0000-000000000012" "$RESULT"

# ---- Test 5: no new videos left → returns NULL ----
RESULT=$("${PSQL[@]}" -At -c "SELECT id FROM automation.reserve_next_video();")
assert_eq "no new videos returns empty" "" "$RESULT"

# ---- Test 6: already-reserved and failed videos were never touched ----
COUNT=$("${PSQL[@]}" -At -c "
    SELECT count(*) FROM automation.videos
    WHERE id IN (
        'ff000000-0000-0000-0000-000000000013',
        'ff000000-0000-0000-0000-000000000014'
    ) AND status IN ('reserved', 'failed');
")
assert_eq "non-new videos untouched" "2" "$COUNT"

# ---- Test 7: function returns status = 'reserved' in the returned row ----
# Reset vid_a to new for this test
"${PSQL[@]}" -c "
    UPDATE automation.videos SET status = 'new'
    WHERE id = 'ff000000-0000-0000-0000-000000000010';
" >/dev/null
RESULT=$("${PSQL[@]}" -At -c "SELECT status FROM automation.reserve_next_video();")
assert_eq "returned row has status='reserved'" "reserved" "$RESULT"

# ---- Test 8: concurrent reservation — two sessions never get the same video ----
# Reset all three test videos to new
"${PSQL[@]}" -c "
    UPDATE automation.videos SET status = 'new'
    WHERE id IN (
        'ff000000-0000-0000-0000-000000000010',
        'ff000000-0000-0000-0000-000000000011',
        'ff000000-0000-0000-0000-000000000012'
    );
" >/dev/null

# Launch two concurrent psql processes that each try to reserve a video
TMPDIR_TEST=$(mktemp -d)
(
    "${PSQL[@]}" -At -c "SELECT id FROM automation.reserve_next_video();" \
        > "$TMPDIR_TEST/session1.txt" 2>&1
) &
PID1=$!
(
    "${PSQL[@]}" -At -c "SELECT id FROM automation.reserve_next_video();" \
        > "$TMPDIR_TEST/session2.txt" 2>&1
) &
PID2=$!
wait $PID1 $PID2

S1=$(cat "$TMPDIR_TEST/session1.txt")
S2=$(cat "$TMPDIR_TEST/session2.txt")
rm -rf "$TMPDIR_TEST"

# Both should have a result (3 new videos, 2 reservations)
if [[ -n "$S1" && -n "$S2" && "$S1" != "$S2" ]]; then
    echo "  PASS: true concurrent reservation — different videos ($S1 vs $S2)"
    PASS=$((PASS + 1))
else
    echo "  FAIL: true concurrent reservation (s1='$S1', s2='$S2')"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
echo "PASS: video reservation tests"
