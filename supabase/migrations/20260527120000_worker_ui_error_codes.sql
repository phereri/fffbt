-- Add worker Mobile UI error codes raised by proof_of_posting.
-- These codes are non-infrastructure outcomes that should be recorded cleanly
-- through automation.process_job_error() instead of crashing the scheduler.

INSERT INTO automation.error_catalog
    (error_code, category, target_job_status, max_retries, account_side_effect, description)
VALUES
    ('caption_mismatch',                'needs_review', 'needs_review', 0, NULL, 'Caption field did not match expected caption before sharing'),
    ('share_did_not_register',          'needs_review', 'needs_review', 0, NULL, 'Instagram Share tap did not register or could not be confirmed'),
    ('trial_reels_gallery_not_reached', 'needs_review', 'needs_review', 0, NULL, 'Trial Reels gallery was not reached after create navigation'),
    ('share_screen_not_reached',        'needs_review', 'needs_review', 0, NULL, 'Instagram Share screen was not reached after editor navigation'),
    ('editor_next_not_reached',         'needs_review', 'needs_review', 0, NULL, 'Instagram editor Next transition did not complete'),
    ('next_button_inactive',            'needs_review', 'needs_review', 0, NULL, 'Instagram editor Next button was visible but inactive')
ON CONFLICT (error_code) DO UPDATE SET
    category = EXCLUDED.category,
    target_job_status = EXCLUDED.target_job_status,
    max_retries = EXCLUDED.max_retries,
    account_side_effect = EXCLUDED.account_side_effect,
    description = EXCLUDED.description;
