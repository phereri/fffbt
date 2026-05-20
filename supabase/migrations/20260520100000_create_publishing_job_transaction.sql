-- Atomic publishing job creation (FFF-15).
--
-- Orchestrates the full reservation pipeline inside a single transaction:
--   1. Reserve the oldest available video.
--   2. Find an eligible account (+ environment).
--   3. Reserve a physical device.
--   4. Create the job and audit records.
--
-- If any resource is unavailable, prior reservations within this call are
-- explicitly rolled back so nothing is left dangling.
-- If a concurrent scheduler wins a race (unique_violation on the partial
-- indexes), the EXCEPTION handler rolls back the implicit savepoint.
--
-- Returns the created job row, or NULL when no job could be created.
--
-- Usage:
--   SELECT * FROM automation.create_publishing_job();

CREATE OR REPLACE FUNCTION automation.create_publishing_job()
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
    -- 1. Reserve video (FOR UPDATE SKIP LOCKED inside).
    SELECT * INTO v_video FROM automation.reserve_next_video();
    IF v_video.id IS NULL THEN
        RETURN NULL;
    END IF;

    -- 2. Find eligible account.
    SELECT account_id, environment_id
      INTO v_account_id, v_environment_id
      FROM automation.find_eligible_account();

    IF v_account_id IS NULL THEN
        UPDATE automation.videos SET status = 'new' WHERE id = v_video.id;
        RETURN NULL;
    END IF;

    -- 3. Reserve physical device.
    v_device_id := automation.reserve_physical_device();
    IF v_device_id IS NULL THEN
        UPDATE automation.videos SET status = 'new' WHERE id = v_video.id;
        RETURN NULL;
    END IF;

    -- 4. Create job (partial unique indexes guard against double-booking).
    INSERT INTO automation.jobs (video_id, account_id, environment_id, device_id, status)
    VALUES (v_video.id, v_account_id, v_environment_id, v_device_id, 'queued')
    RETURNING id INTO v_job_id;

    -- 5. Audit: job creation event.
    INSERT INTO automation.job_events (job_id, event_type, to_status, payload)
    VALUES (v_job_id, 'created', 'queued', jsonb_build_object(
        'video_id', v_video.id,
        'account_id', v_account_id,
        'environment_id', v_environment_id,
        'device_id', v_device_id
    ));

    -- 6. Link device to the new job.
    UPDATE automation.physical_devices
       SET current_job_id = v_job_id
     WHERE id = v_device_id;

    -- 7. Device assignment event.
    INSERT INTO automation.device_events (device_id, event_type, payload)
    VALUES (v_device_id, 'job_assigned', jsonb_build_object('job_id', v_job_id));

    SELECT * INTO v_job FROM automation.jobs WHERE id = v_job_id;
    RETURN v_job;

EXCEPTION
    WHEN unique_violation THEN
        RETURN NULL;
END;
$$;
