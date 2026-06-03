-- Isolate validation/local videos from the generic Drive queue.
--
-- automation.reserve_next_video() picks the oldest video with status='new'.
-- Without a filter, validation videos (seeded via scripts/seed_validation_video.py
-- with category='validation' and download_method='local_validation') would be
-- picked up by the launcher's generic loop and consume an account+device for
-- something that is meant to be triggered manually via targeted create-job.
--
-- After this migration, reserve_next_video() ignores any row that is either:
--   - tagged as a validation video (category = 'validation'), OR
--   - marked as local-validation seed (download_method = 'local_validation')
--
-- Targeted job creation still picks these videos when --video-id is passed
-- explicitly (see fffbt create-job --device-serial --video-id <uuid>).

CREATE OR REPLACE FUNCTION automation.reserve_next_video()
RETURNS automation.videos
LANGUAGE plpgsql
AS $$
DECLARE
    v_video automation.videos;
BEGIN
    SELECT * INTO v_video
    FROM automation.videos
    WHERE status = 'new'
      AND COALESCE(category, '') <> 'validation'
      AND COALESCE(download_method, '') <> 'local_validation'
    ORDER BY created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF v_video.id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE automation.videos
    SET status = 'reserved'
    WHERE id = v_video.id;

    v_video.status := 'reserved';
    v_video.updated_at := now();

    RETURN v_video;
END;
$$;
