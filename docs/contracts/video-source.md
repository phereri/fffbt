# Video source contract

- Status: accepted (MVP)
- Owner: Drive Ingestor
- Last updated: 2026-05-18

This document defines the contract for discovering, downloading, and registering
video files from Google Drive into `automation.videos`.

## Source

Google Drive folder structure:

```
instagram/<category-folder>/videos/*.mp4
```

Only `.mp4` files are in scope for MVP. All other file types are ignored.

`<category-folder>` is a subfolder name (e.g. `fitness`, `cooking`). The
ingestor derives the `category` column from this folder name.

## Discovery

The ingestor uses the Google Drive API (v3) with a service account
(`GOOGLE_APPLICATION_CREDENTIALS`). It lists files under the configured root
folder, filtering by:

- `mimeType = 'video/mp4'`
- parent path matches `instagram/*/videos/`

The Google Drive `file.id` is the **duplicate key**. Before inserting, the
ingestor checks `automation.videos.google_drive_file_id` for an existing row.
If a match exists, the file is skipped (idempotent scan).

## Metadata

Every discovered file produces one row in `automation.videos` with these fields:

| Column | Source | Example |
|--------|--------|---------|
| `google_drive_file_id` | Drive API `file.id` | `1aBcDeFgHiJkLmNoPqRsT` |
| `google_drive_folder_id` | Drive API `file.parents[0]` | `0BxYzAbCdEfGhIjKlMnOp` |
| `source_path` | Derived from folder hierarchy | `instagram/fitness/videos/` |
| `filename` | Drive API `file.name` | `clip_001.mp4` |
| `extension` | Parsed from filename | `mp4` |
| `mime_type` | Drive API `file.mimeType` | `video/mp4` |
| `size_bytes` | Drive API `file.size` | `15728640` |
| `category` | Parsed from `<category-folder>` | `fitness` |
| `platform` | Hardcoded for MVP | `instagram` |
| `download_method` | How the file was fetched | `google_drive_api` |

## Download

A Google Drive link is **not** sufficient for the downstream worker. The Poster
(Appium worker) needs a local file path on the machine running the job.

The ingestor downloads each new file to:

```
$VIDEO_DOWNLOAD_DIR/<google_drive_file_id>.mp4
```

Using the Drive file ID as the local filename avoids collisions when different
category folders contain files with the same name. The `.mp4` extension is
preserved for tooling that inspects file extensions.

After a successful download the ingestor writes the absolute path to
`automation.videos.local_video_path`. Downstream consumers (e.g.
`prepare_video_for_android`) read this column to locate the file.

If the download fails, the row is **not** inserted. A partial download is
cleaned up (deleted) before the next retry.

## Status lifecycle (ingestion phase)

The ingestor only writes rows with `status = 'new'`. Later status transitions
(`reserved`, `uploading`, `verifying`, `released`, `failed`, `needs_review`)
are owned by the scheduler and job components, not by the ingestor.

```
Drive scan → file discovered
           → duplicate check (google_drive_file_id)
           → download to VIDEO_DOWNLOAD_DIR
           → INSERT into automation.videos (status = 'new')
```

## Idempotency

Scans are safe to re-run at any time:

1. **Duplicate detection**: `google_drive_file_id` has a unique constraint.
   Files already in the database are skipped.
2. **Partial failure**: If a scan is interrupted, the next run picks up where
   it left off — unregistered files are discovered again, already-registered
   files are skipped.
3. **Renames/moves**: A renamed file in Drive keeps the same `file.id`, so it
   is correctly detected as a duplicate. A file moved to a different category
   folder is also detected by `file.id` — the existing row is not updated
   (the original category stands).
4. **Re-uploads**: A user who deletes and re-uploads the same video gets a new
   Drive `file.id`, which means a new row. This is intentional — the platform
   treats it as a new video.

## Credentials

- The ingestor reads `GOOGLE_APPLICATION_CREDENTIALS` (path to the service
  account JSON key file).
- The JSON file must never be committed, logged, or surfaced in issue comments.
- See `docs/setup/credentials.md` for key rotation and deployment guidance.

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `GOOGLE_APPLICATION_CREDENTIALS` | yes | — | Path to service account JSON. |
| `VIDEO_DOWNLOAD_DIR` | no | `./.artifacts/videos` | Where downloaded `.mp4` files are stored locally. |
