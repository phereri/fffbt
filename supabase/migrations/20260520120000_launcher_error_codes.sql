-- Add launcher infrastructure error codes to error_catalog (FFF-53).
-- These cover failures raised by the launcher itself (connection issues,
-- timeouts, unknown exceptions, heartbeat staleness) as opposed to
-- worker/Instagram errors already catalogued by FFF-17.

INSERT INTO automation.error_catalog
    (error_code, category, target_job_status, max_retries, account_side_effect, description)
VALUES
    ('INFRA',             'retryable',     'failed',       3, NULL, 'Launcher infrastructure error (connection, OS)'),
    ('TIMEOUT',           'retryable',     'failed',       2, NULL, 'Job exceeded the launcher timeout'),
    ('UNKNOWN',           'needs_review',  'needs_review', 0, NULL, 'Unhandled exception in worker'),
    ('HEARTBEAT_TIMEOUT', 'needs_review',  'needs_review', 0, NULL, 'No heartbeat received within timeout window');
