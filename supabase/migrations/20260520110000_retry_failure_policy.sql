-- Retry and failure policy: error catalog, retry tracking, and process_job_error().
-- Contract: docs/contracts/retry-failure-policy.md

-- 1. Add retry tracking to jobs
ALTER TABLE automation.jobs
    ADD COLUMN retry_count integer NOT NULL DEFAULT 0;

-- 2. Error catalog: encodes the retry/failure policy per error code
CREATE TABLE automation.error_catalog (
    error_code          text PRIMARY KEY,
    category            text NOT NULL,
    target_job_status   text NOT NULL,
    max_retries         integer NOT NULL DEFAULT 0,
    account_side_effect text,
    description         text NOT NULL,

    CONSTRAINT error_catalog_category_chk CHECK (
        category IN ('retryable', 'needs_review', 'non_retryable')
    ),
    CONSTRAINT error_catalog_target_chk CHECK (
        target_job_status IN ('failed', 'needs_review')
    ),
    CONSTRAINT error_catalog_max_retries_chk CHECK (max_retries >= 0),
    CONSTRAINT error_catalog_side_effect_chk CHECK (
        account_side_effect IS NULL
        OR account_side_effect IN ('disabled', 'suspended', 'banned')
    )
);

-- 3. Seed error catalog
INSERT INTO automation.error_catalog (error_code, category, target_job_status, max_retries, account_side_effect, description) VALUES
    -- Retryable (infrastructure / transient)
    ('proxy_failed',          'retryable',      'failed',       3, NULL,        'Proxy connection or authentication error'),
    ('device_profile_failed', 'retryable',      'failed',       2, NULL,        'Device fingerprint injection failed'),
    ('gps_failed',            'retryable',      'failed',       2, NULL,        'MockGPS setup or injection failed'),
    ('login_required',        'retryable',      'failed',       1, NULL,        'Instagram session expired, re-login needed'),
    ('upload_failed',         'retryable',      'failed',       3, NULL,        'Instagram upload error (network/timeout)'),
    ('device_offline',        'retryable',      'failed',       2, NULL,        'Physical device unreachable via ADB'),
    -- Needs review (ambiguous)
    ('captcha',               'needs_review',   'needs_review', 0, NULL,        'Captcha challenge detected'),
    ('verification_failed',   'needs_review',   'needs_review', 0, NULL,        'Could not confirm post was published'),
    ('unknown_screen',        'needs_review',   'needs_review', 0, NULL,        'Unrecognized Instagram UI state'),
    -- Non-retryable (hard business failures)
    ('logged_out',              'non_retryable', 'failed', 0, 'disabled',  'Instagram forced logout'),
    ('trial_reels_unavailable', 'non_retryable', 'failed', 0, NULL,        'Trial Reels feature not available for account'),
    ('suspended',               'non_retryable', 'failed', 0, 'suspended', 'Instagram suspended the account'),
    ('checkpoint',              'non_retryable', 'failed', 0, 'disabled',  'Instagram checkpoint/security verification'),
    ('two_factor',              'non_retryable', 'failed', 0, 'disabled',  '2FA challenge encountered'),
    ('action_blocked',          'non_retryable', 'failed', 0, NULL,        'Instagram action block (temporary)');

-- 4. Add global setting for default max retries
INSERT INTO automation.global_settings (key, value, description) VALUES
    ('max_retries_default', '3', 'Fallback max retries for error codes not in catalog')
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    description = EXCLUDED.description;

-- 5. process_job_error(): high-level error handler that workers call.
--    Encodes the entire retry/failure policy. Returns a JSONB summary of action taken.
--    Terminal paths (failed, needs_review) release the physical device and
--    move the video out of any active state so resources are never stuck.
CREATE OR REPLACE FUNCTION automation.process_job_error(
    p_job_id        uuid,
    p_error_code    text,
    p_error_message text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    v_catalog             automation.error_catalog%ROWTYPE;
    v_job                 automation.jobs%ROWTYPE;
    v_message             text;
    v_result              jsonb;
    v_terminal            boolean := false;
    v_video_cleanup_status text;
    v_released_device_id  uuid;
BEGIN
    SELECT * INTO v_catalog
    FROM automation.error_catalog
    WHERE error_code = p_error_code;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown error code: %', p_error_code;
    END IF;

    SELECT * INTO v_job
    FROM automation.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'job not found: %', p_job_id;
    END IF;

    v_message := COALESCE(p_error_message, v_catalog.description);

    IF v_catalog.category = 'retryable' AND v_job.retry_count < v_catalog.max_retries THEN
        PERFORM automation.transition_job_status(
            p_job_id, 'failed',
            jsonb_build_object(
                'error_code', p_error_code,
                'error_message', v_message
            )
        );

        UPDATE automation.jobs
        SET retry_count = retry_count + 1
        WHERE id = p_job_id;

        PERFORM automation.transition_job_status(
            p_job_id, 'queued',
            jsonb_build_object(
                'retry', true,
                'retry_count', v_job.retry_count + 1,
                'error_code', p_error_code
            )
        );

        INSERT INTO automation.job_events (job_id, event_type, from_status, to_status, payload)
        VALUES (p_job_id, 'retry', 'failed', 'queued',
            jsonb_build_object(
                'retry_count', v_job.retry_count + 1,
                'max_retries', v_catalog.max_retries,
                'error_code', p_error_code,
                'error_message', v_message
            )
        );

        v_result := jsonb_build_object(
            'action', 'retried',
            'retry_count', v_job.retry_count + 1,
            'max_retries', v_catalog.max_retries
        );

    ELSIF v_catalog.target_job_status = 'needs_review' THEN
        PERFORM automation.transition_job_status(
            p_job_id, 'needs_review',
            jsonb_build_object(
                'error_code', p_error_code,
                'error_message', v_message
            )
        );

        UPDATE automation.jobs
        SET error_code    = p_error_code,
            error_message = v_message
        WHERE id = p_job_id;

        v_terminal := true;
        v_video_cleanup_status := 'needs_review';
        v_result := jsonb_build_object('action', 'needs_review');

    ELSE
        PERFORM automation.transition_job_status(
            p_job_id, 'failed',
            jsonb_build_object(
                'error_code', p_error_code,
                'error_message', v_message
            )
        );

        v_terminal := true;
        v_video_cleanup_status := 'new';

        IF v_catalog.category = 'retryable' THEN
            v_result := jsonb_build_object(
                'action', 'retries_exhausted',
                'retry_count', v_job.retry_count,
                'max_retries', v_catalog.max_retries
            );
        ELSE
            v_result := jsonb_build_object('action', 'terminal_failure');
        END IF;
    END IF;

    -- Terminal resource cleanup: release device and video so nothing stays stuck.
    IF v_terminal THEN
        INSERT INTO automation.job_events (job_id, event_type, payload)
        VALUES (p_job_id, 'error',
            jsonb_build_object(
                'error_code', p_error_code,
                'error_message', v_message
            )
        );

        UPDATE automation.physical_devices
        SET status = 'online', current_job_id = NULL
        WHERE current_job_id = p_job_id
        RETURNING id INTO v_released_device_id;

        IF v_released_device_id IS NOT NULL THEN
            INSERT INTO automation.device_events (device_id, event_type, payload)
            VALUES (v_released_device_id, 'job_released',
                jsonb_build_object('job_id', p_job_id::text)
            );
        END IF;

        UPDATE automation.videos
        SET status = v_video_cleanup_status
        WHERE id = v_job.video_id;
    END IF;

    IF v_catalog.account_side_effect IS NOT NULL THEN
        UPDATE automation.accounts
        SET status = v_catalog.account_side_effect
        WHERE id = v_job.account_id;
    END IF;

    RETURN v_result;
END;
$$;
