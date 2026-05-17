-- Video and job tables for automation schema.
-- Videos drive the queue. Jobs link video + account + environment + device.

-- videos: video files ingested from Google Drive
CREATE TABLE automation.videos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    google_drive_file_id text,
    google_drive_folder_id text,
    source_path text NOT NULL,
    filename text NOT NULL,
    extension text NOT NULL DEFAULT 'mp4',
    mime_type text NOT NULL DEFAULT 'video/mp4',
    size_bytes bigint,
    checksum text,
    platform text NOT NULL DEFAULT 'instagram',
    category text,
    status text NOT NULL DEFAULT 'new',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT videos_status_chk CHECK (status IN (
        'new', 'reserved', 'uploading', 'verifying', 'released', 'failed', 'needs_review'
    )),
    CONSTRAINT videos_platform_chk CHECK (platform IN ('instagram')),
    CONSTRAINT videos_google_drive_file_uq UNIQUE (google_drive_file_id)
);

-- jobs: publishing jobs linking video, account, environment, and device
CREATE TABLE automation.jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id uuid NOT NULL REFERENCES automation.videos(id),
    account_id uuid NOT NULL REFERENCES automation.accounts(id),
    environment_id uuid NOT NULL REFERENCES automation.account_environments(id),
    device_id uuid NOT NULL REFERENCES automation.physical_devices(id),
    status text NOT NULL DEFAULT 'queued',
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT jobs_status_chk CHECK (status IN (
        'queued', 'preparing_device', 'publishing', 'verifying',
        'done', 'failed', 'needs_review', 'cancelled'
    ))
);

-- Partial unique indexes to prevent conflicting active jobs (FFF-9)
CREATE UNIQUE INDEX jobs_video_active_uq
    ON automation.jobs (video_id)
    WHERE status NOT IN ('done', 'failed', 'cancelled');

CREATE UNIQUE INDEX jobs_account_active_uq
    ON automation.jobs (account_id)
    WHERE status NOT IN ('done', 'failed', 'cancelled');

CREATE UNIQUE INDEX jobs_device_active_uq
    ON automation.jobs (device_id)
    WHERE status NOT IN ('done', 'failed', 'cancelled');

-- FK from physical_devices.current_job_id now that jobs exists
ALTER TABLE automation.physical_devices
    ADD CONSTRAINT physical_devices_current_job_fk
    FOREIGN KEY (current_job_id) REFERENCES automation.jobs(id);

-- job_events: audit log for job state transitions
CREATE TABLE automation.job_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES automation.jobs(id),
    event_type text NOT NULL,
    from_status text,
    to_status text,
    payload jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT job_events_event_type_chk CHECK (event_type IN (
        'created', 'status_changed', 'heartbeat', 'error',
        'screenshot_taken', 'verification_started', 'verification_passed',
        'verification_failed', 'retry', 'cancelled'
    ))
);

-- artifacts: screenshots, logs, and other files produced by jobs
CREATE TABLE automation.artifacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES automation.jobs(id),
    artifact_type text NOT NULL,
    file_path text,
    file_url text,
    mime_type text,
    size_bytes bigint,
    metadata jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT artifacts_type_chk CHECK (artifact_type IN (
        'screenshot', 'log', 'video_thumbnail', 'trajectory', 'gif', 'other'
    ))
);

-- Indexes for queue queries
CREATE INDEX videos_status_idx ON automation.videos (status);
CREATE INDEX videos_platform_category_idx ON automation.videos (platform, category);
CREATE INDEX jobs_status_idx ON automation.jobs (status);
CREATE INDEX jobs_video_id_idx ON automation.jobs (video_id);
CREATE INDEX jobs_account_id_idx ON automation.jobs (account_id);
CREATE INDEX jobs_device_id_idx ON automation.jobs (device_id);
CREATE INDEX jobs_created_at_idx ON automation.jobs (created_at);
CREATE INDEX job_events_job_id_idx ON automation.job_events (job_id);
CREATE INDEX job_events_created_at_idx ON automation.job_events (created_at);
CREATE INDEX artifacts_job_id_idx ON automation.artifacts (job_id);

-- updated_at triggers
CREATE TRIGGER trg_videos_updated_at
    BEFORE UPDATE ON automation.videos
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();

CREATE TRIGGER trg_jobs_updated_at
    BEFORE UPDATE ON automation.jobs
    FOR EACH ROW EXECUTE FUNCTION automation.set_updated_at();
