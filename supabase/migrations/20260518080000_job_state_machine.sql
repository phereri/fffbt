-- Job state machine: transition helper with validation and audit logging.
--
-- Usage:
--   SELECT automation.transition_job_status(
--       p_job_id     := '...',
--       p_new_status := 'publishing',
--       p_payload    := '{"note": "upload complete"}'::jsonb
--   );

CREATE OR REPLACE FUNCTION automation.transition_job_status(
    p_job_id     uuid,
    p_new_status text,
    p_payload    jsonb DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_current_status text;
    v_allowed        text[];
BEGIN
    -- Lock the job row to prevent concurrent transitions.
    SELECT status INTO v_current_status
    FROM automation.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'job not found: %', p_job_id;
    END IF;

    -- Define allowed transitions.
    v_allowed := CASE v_current_status
        WHEN 'queued'           THEN ARRAY['preparing_device', 'failed', 'cancelled']
        WHEN 'preparing_device' THEN ARRAY['publishing', 'failed', 'needs_review', 'cancelled']
        WHEN 'publishing'       THEN ARRAY['verifying', 'failed', 'needs_review']
        WHEN 'verifying'        THEN ARRAY['done', 'failed', 'needs_review']
        WHEN 'needs_review'     THEN ARRAY['queued', 'cancelled', 'failed']
        WHEN 'failed'           THEN ARRAY['queued']
        ELSE ARRAY[]::text[]
    END;

    IF p_new_status <> ALL(v_allowed) THEN
        RAISE EXCEPTION 'invalid transition: % -> %', v_current_status, p_new_status;
    END IF;

    -- Update job status and relevant timestamps / error fields.
    UPDATE automation.jobs
    SET status        = p_new_status,
        started_at    = CASE
                            WHEN started_at IS NULL AND p_new_status <> 'queued'
                            THEN now()
                            ELSE started_at
                        END,
        finished_at   = CASE
                            WHEN p_new_status IN ('done', 'failed', 'cancelled')
                            THEN now()
                            ELSE NULL
                        END,
        error_code    = CASE
                            WHEN p_new_status = 'failed'
                            THEN p_payload ->> 'error_code'
                            WHEN p_new_status NOT IN ('failed', 'needs_review')
                            THEN NULL
                            ELSE error_code
                        END,
        error_message = CASE
                            WHEN p_new_status = 'failed'
                            THEN p_payload ->> 'error_message'
                            WHEN p_new_status NOT IN ('failed', 'needs_review')
                            THEN NULL
                            ELSE error_message
                        END
    WHERE id = p_job_id;

    -- Write audit event.
    INSERT INTO automation.job_events (job_id, event_type, from_status, to_status, payload)
    VALUES (p_job_id, 'status_changed', v_current_status, p_new_status, p_payload);
END;
$$;
