-- Video reservation query (FFF-13).
--
-- Atomically picks the oldest video with status = 'new' and sets it to 'reserved'.
-- Uses FOR UPDATE SKIP LOCKED so concurrent scheduler runs never reserve the same video.
--
-- Returns the reserved video row, or NULL when no new videos are available.
--
-- Usage:
--   SELECT * FROM automation.reserve_next_video();

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
