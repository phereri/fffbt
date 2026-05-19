-- Account eligibility query helper (FFF-12).
--
-- Returns the single best eligible account for the next publishing job.
--
-- Eligibility rules:
--   1. account.status = 'active'
--   2. account has no active job (status not in done/failed/cancelled)
--   3. account_environment exists with all components active
--   4. completed posts in last 24h < daily_posts_limit_per_account
--   5. cooldown since last completed post has elapsed
--
-- Ordering: oldest last_published_at first (NULLS FIRST = never-posted accounts).
--
-- Usage:
--   SELECT * FROM automation.find_eligible_account();
--   SELECT * FROM automation.find_eligible_account(p_cooldown_seconds := 1800);

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
