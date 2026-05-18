-- Add local_video_path and download_method to automation.videos.
-- The Poster (Appium worker) needs a local file path, not a Drive link.

ALTER TABLE automation.videos
    ADD COLUMN local_video_path text,
    ADD COLUMN download_method text NOT NULL DEFAULT 'google_drive_api';
