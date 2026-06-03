-- Pre-account-login MVP hardening.
--
-- Goal: while the VPS has phones controlled but no Instagram account logged
-- in yet, prevent the generic launcher from grabbing validation/placeholder
-- accounts and prevent duplicate physical_devices rows from accumulating.
--
-- Changes:
--   1. automation.accounts.is_validation boolean — true for seeded
--      placeholder accounts (e.g. validation_happy_path,
--      validation_error_path). Backfilled here for any existing rows whose
--      username starts with "validation_" or whose password is the seed
--      sentinel.
--   2. automation.find_eligible_account() excludes is_validation = true.
--      Targeted job creation that pins --account-id still works because
--      that path bypasses find_eligible_account.
--   3. Partial unique indexes on automation.physical_devices to prevent
--      duplicate rows by Tailscale device_id, ADB serial, or GenFarmer
--      device id (each guarded only when non-null).

ALTER TABLE automation.accounts
    ADD COLUMN IF NOT EXISTS is_validation boolean NOT NULL DEFAULT false;

-- Backfill existing seeded validation accounts.
UPDATE automation.accounts
   SET is_validation = true
 WHERE is_validation = false
   AND (
       username LIKE 'validation\_%'
       OR password = 'VALIDATION_ALREADY_LOGGED_IN_NO_REAL_PASSWORD'
   );

CREATE INDEX IF NOT EXISTS accounts_is_validation_idx
    ON automation.accounts (is_validation);

CREATE OR REPLACE FUNCTION automation.find_eligible_account(
    p_cooldown_seconds int DEFAULT NULL
) RETURNS TABLE (
    account_id uuid,
    environment_id uuid
)
LANGUAGE sql STABLE
AS $$
    WITH settings AS (
        SELECT
            (SELECT value::int FROM automation.global_settings
             WHERE key = 'daily_posts_limit_per_account') AS daily_limit,
            COALESCE(
                p_cooldown_seconds,
                (SELECT value::int FROM automation.global_settings
                 WHERE key = 'post_interval_min_seconds')
            ) AS cooldown_secs
    ),
    account_stats AS (
        SELECT
            j.account_id,
            max(j.finished_at) AS last_published_at,
            count(*) FILTER (
                WHERE j.finished_at >= now() - interval '24 hours'
            ) AS done_last_24h
        FROM automation.jobs j
        WHERE j.status = 'done'
        GROUP BY j.account_id
    )
    SELECT a.id AS account_id, ae.id AS environment_id
    FROM automation.accounts a
    JOIN automation.account_environments ae ON ae.account_id = a.id
    JOIN automation.proxies p ON p.id = ae.proxy_id AND p.status = 'active'
    JOIN automation.device_profiles dp ON dp.id = ae.device_profile_id AND dp.status = 'active'
    JOIN automation.gps_locations gl ON gl.id = ae.gps_location_id AND gl.status = 'active'
    JOIN automation.app_states aps ON aps.id = ae.app_state_id AND aps.status = 'active'
    CROSS JOIN settings s
    LEFT JOIN account_stats st ON st.account_id = a.id
    WHERE a.status = 'active'
      AND COALESCE(a.is_validation, false) = false
      AND NOT EXISTS (
          SELECT 1 FROM automation.jobs j
          WHERE j.account_id = a.id
            AND j.status NOT IN ('done', 'failed', 'cancelled')
      )
      AND COALESCE(st.done_last_24h, 0) < s.daily_limit
      AND (
          st.last_published_at IS NULL
          OR st.last_published_at < now() - make_interval(secs => s.cooldown_secs)
      )
    ORDER BY st.last_published_at ASC NULLS FIRST
    LIMIT 1;
$$;

-- Partial unique indexes prevent duplicate physical_devices rows.
-- Each is non-null-guarded so legacy rows that left these fields NULL are
-- not blocked from coexisting.

CREATE UNIQUE INDEX IF NOT EXISTS physical_devices_device_id_uq
    ON automation.physical_devices (device_id)
    WHERE device_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS physical_devices_adb_serial_uq
    ON automation.physical_devices (adb_serial)
    WHERE adb_serial IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS physical_devices_genfarmer_device_id_uq
    ON automation.physical_devices (genfarmer_device_id)
    WHERE genfarmer_device_id IS NOT NULL;
