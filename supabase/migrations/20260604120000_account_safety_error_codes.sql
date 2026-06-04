-- Add the account-safety hard-stop codes the worker actually emits.
--
-- The Mobile UI step + agent result mapper emit `login_challenge`,
-- `account_suspended`, and `unexpected_destructive_dialog` (see
-- src/worker/steps/mobile_ui_automation.py `_HARD_STOP_PATTERNS` and
-- src/worker/agent_runner/mobilerun_agent_runner.py `_FAILURE_REASON_MAP`).
-- These were absent from the catalog — only the synonyms `two_factor`,
-- `checkpoint`, and `suspended` existed, which the worker never emits.
--
-- automation.process_job_error() RAISES on an unknown code, so an
-- uncatalogued hard-stop left the job stuck (device not released until the
-- heartbeat reaper fired) and never applied the account side effect. Catalog
-- them so they route deterministically:
--   * login_challenge  -> non-retryable, disable account (matches two_factor/
--     checkpoint policy). Does NOT retry endlessly.
--   * account_suspended -> non-retryable, suspend account (matches `suspended`).
--   * unexpected_destructive_dialog -> needs_review (a destructive dialog
--     appeared unprompted: anomalous, a human should inspect before reuse).
--     No automatic account side effect — the dialog alone is not proof the
--     account is bad.
--
-- The legacy synonyms (two_factor, checkpoint, suspended) are left in place;
-- removing them is out of scope for this fix.

INSERT INTO automation.error_catalog
    (error_code, category, target_job_status, max_retries, account_side_effect, description)
VALUES
    ('login_challenge',                'non_retryable', 'failed',       0, 'disabled',  'Instagram 2FA / security code / verify-identity / checkpoint challenge'),
    ('account_suspended',              'non_retryable', 'failed',       0, 'suspended', 'Instagram suspended/disabled the account'),
    ('unexpected_destructive_dialog',  'needs_review',  'needs_review', 0, NULL,        'Unexpected destructive dialog (logout-all / delete) — human review required')
ON CONFLICT (error_code) DO UPDATE SET
    category = EXCLUDED.category,
    target_job_status = EXCLUDED.target_job_status,
    max_retries = EXCLUDED.max_retries,
    account_side_effect = EXCLUDED.account_side_effect,
    description = EXCLUDED.description;
