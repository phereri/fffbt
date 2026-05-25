-- Add caption and hashtags columns to jobs for audit / debugging (FFF-33).
ALTER TABLE automation.jobs
    ADD COLUMN caption text,
    ADD COLUMN hashtags text[];
