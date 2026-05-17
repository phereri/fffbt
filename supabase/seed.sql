INSERT INTO automation.global_settings (key, value, description) VALUES
    ('daily_posts_limit_per_account', '20', 'Maximum posts per account per day'),
    ('post_interval_min_seconds', '900', 'Minimum delay between posts in seconds'),
    ('post_interval_max_seconds', '3600', 'Maximum delay between posts in seconds'),
    ('verification_delay_seconds', '180', 'Seconds to wait before verifying a post'),
    ('max_parallel_jobs', '20', 'Maximum number of concurrent jobs'),
    ('job_heartbeat_timeout_seconds', '120', 'Seconds before a job without heartbeat is considered stale')
ON CONFLICT (key) DO UPDATE SET
    value = EXCLUDED.value,
    description = EXCLUDED.description;
