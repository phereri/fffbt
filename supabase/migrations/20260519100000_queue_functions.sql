-- Queue functions adapted from old CRM patterns (FFF-51).
--
-- Extracted ideas from public.automation_tasks / claim_next_task:
--   - FOR UPDATE SKIP LOCKED for concurrent-safe reservation
--   - Structured per-job operational logging (job_logs)
--   - Device-based filtering (replaces old host_id)
--   - Trajectory data via artifacts (artifact_type = 'trajectory', metadata)

----------------------------------------------------------------------------
-- 1. job_logs: structured operational logging per job
--    Adapted from public.automation_task_logs (level, source, message).
--    Complements job_events (audit) with verbose runtime/debug logging.
----------------------------------------------------------------------------
CREATE TABLE automation.job_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES automation.jobs(id) ON DELETE CASCADE,
    ts timestamptz NOT NULL DEFAULT now(),
    level text NOT NULL DEFAULT 'info',
    source text,
    message text NOT NULL,

    CONSTRAINT job_logs_level_chk CHECK (level IN ('debug', 'info', 'warn', 'error'))
);

CREATE INDEX job_logs_job_id_ts_idx ON automation.job_logs (job_id, ts);

----------------------------------------------------------------------------
-- 2. claim_next_job: atomic job claiming for device workers
--    Adapted from public.claim_next_task(p_host_id).
--    Uses FOR UPDATE SKIP LOCKED so concurrent workers never deadlock.
--    Transitions job via the existing state machine and reserves the device.
--
--    Returns NULL when no queued job exists for the device.
----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION automation.claim_next_job(p_device_id uuid)
RETURNS automation.jobs
LANGUAGE plpgsql
AS $$
DECLARE
    v_job_id uuid;
    v_job    automation.jobs;
BEGIN
    SELECT id INTO v_job_id
    FROM automation.jobs
    WHERE status = 'queued'
      AND device_id = p_device_id
    ORDER BY created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF v_job_id IS NULL THEN
        RETURN NULL;
    END IF;

    PERFORM automation.transition_job_status(v_job_id, 'preparing_device');

    UPDATE automation.physical_devices
    SET status = 'busy',
        current_job_id = v_job_id
    WHERE id = p_device_id;

    INSERT INTO automation.device_events (device_id, event_type, payload)
    VALUES (p_device_id, 'job_assigned', jsonb_build_object('job_id', v_job_id));

    SELECT * INTO v_job
    FROM automation.jobs
    WHERE id = v_job_id;

    RETURN v_job;
END;
$$;
