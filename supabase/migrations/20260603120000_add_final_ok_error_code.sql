-- Add the final_ok_did_not_register error code raised by proof_of_posting when
-- the Trial Reel "New reel" final screen does not transition after tapping the
-- top-right OK button. This is the Trial Reel publish action; failure to
-- confirm should NOT be misclassified as share_did_not_register or
-- trial_reels_unavailable.

INSERT INTO automation.error_catalog
    (error_code, category, target_job_status, max_retries, account_side_effect, description)
VALUES
    (
        'final_ok_did_not_register',
        'needs_review',
        'needs_review',
        0,
        NULL,
        'Trial Reel publish OK tap did not produce a screen transition'
    )
ON CONFLICT (error_code) DO UPDATE SET
    category = EXCLUDED.category,
    target_job_status = EXCLUDED.target_job_status,
    max_retries = EXCLUDED.max_retries,
    account_side_effect = EXCLUDED.account_side_effect,
    description = EXCLUDED.description;
