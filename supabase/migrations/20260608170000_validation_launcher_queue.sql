-- Validation launcher queue (opt-in, isolated from the production Drive queue).
--
-- The default launcher (`run-launcher`) uses automation.create_publishing_job()
-- -> reserve_next_video() which EXCLUDES validation videos (migration
-- 20260603130000) and find_eligible_account() which EXCLUDES is_validation
-- accounts (migration 20260603140000). That keeps the production loop off the
-- seeded validation/local videos and placeholder accounts.
--
-- This migration adds a PARALLEL, opt-in validation path used only by
-- `run-launcher --queue validation`. It does NOT change any default behavior:
-- reserve_next_video(), find_eligible_account(), and create_publishing_job()
-- are left untouched. The validation path selects the MIRROR set and never
-- selects google_drive_api/slop production videos or production accounts:
--   videos:   status='new' AND (category='validation' OR download_method='local_validation')
--   accounts: status='active' AND is_validation = true (otherwise same env/limits)
--   devices:  same automation.reserve_physical_device() (online + fresh heartbeat)

-- 1. Validation video reservation — the inverse filter of reserve_next_video().
CREATE OR REPLACE FUNCTION automation.reserve_next_validation_video()
RETURNS automation.videos
LANGUAGE plpgsql
AS $$
DECLARE
    v_video automation.videos;
BEGIN
    SELECT * INTO v_video
    FROM automation.videos
    WHERE status = 'new'
      AND (
          COALESCE(category, '') = 'validation'
          OR COALESCE(download_method, '') = 'local_validation'
      )
    ORDER BY created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF v_video.id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE automation.videos
    SET status = 'reserved'
    WHERE id = v_video.id;

    v_video.status := 'reserved';
    v_video.updated_at := now();
    RETURN v_video;
END;
$$;

-- 2. Validation account eligibility — is_validation = true; otherwise identical
--    rules to find_eligible_account (active environment, daily limit, cooldown).
CREATE OR REPLACE FUNCTION automation.find_eligible_validation_account(
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
      AND COALESCE(a.is_validation, false) = true
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

-- 3. Validation publishing-job orchestrator — a mirror of
--    automation.create_publishing_job() that uses the validation video +
--    validation account paths. Tags job_events/device_events payload with
--    queue='validation' so the queue choice is visible in the audit trail.
CREATE OR REPLACE FUNCTION automation.create_validation_publishing_job()
RETURNS automation.jobs
LANGUAGE plpgsql
AS $$
DECLARE
    v_video          automation.videos;
    v_account_id     uuid;
    v_environment_id uuid;
    v_device_id      uuid;
    v_job_id         uuid;
    v_job            automation.jobs;
BEGIN
    -- 1. Reserve a validation video (FOR UPDATE SKIP LOCKED inside).
    SELECT * INTO v_video FROM automation.reserve_next_validation_video();
    IF v_video.id IS NULL THEN
        RETURN NULL;
    END IF;

    -- 2. Find an eligible validation account.
    SELECT account_id, environment_id
      INTO v_account_id, v_environment_id
      FROM automation.find_eligible_validation_account();
    IF v_account_id IS NULL THEN
        UPDATE automation.videos SET status = 'new' WHERE id = v_video.id;
        RETURN NULL;
    END IF;

    -- 3. Reserve a physical device (shared with production; online + fresh).
    v_device_id := automation.reserve_physical_device();
    IF v_device_id IS NULL THEN
        UPDATE automation.videos SET status = 'new' WHERE id = v_video.id;
        RETURN NULL;
    END IF;

    -- 4. Create job.
    INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status)
    VALUES (v_video.id, v_account_id, v_environment_id, v_device_id, 'queued')
    RETURNING id INTO v_job_id;

    -- 5. Audit: job creation event (queue tagged).
    INSERT INTO automation.job_events (job_id, event_type, to_status, payload)
    VALUES (v_job_id, 'created', 'queued', jsonb_build_object(
        'video_id', v_video.id,
        'account_id', v_account_id,
        'environment_id', v_environment_id,
        'device_id', v_device_id,
        'queue', 'validation'
    ));

    -- 6. Link device to the new job.
    UPDATE automation.physical_devices
       SET current_job_id = v_job_id
     WHERE id = v_device_id;

    -- 7. Device assignment event (queue tagged).
    INSERT INTO automation.device_events (device_id, event_type, payload)
    VALUES (v_device_id, 'job_assigned',
            jsonb_build_object('job_id', v_job_id, 'queue', 'validation'));

    SELECT * INTO v_job FROM automation.jobs WHERE id = v_job_id;
    RETURN v_job;

EXCEPTION
    WHEN unique_violation THEN
        RETURN NULL;
END;
$$;
