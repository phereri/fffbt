#!/usr/bin/env python3
"""Sync video files from Google Drive into automation.videos.

Scans instagram/<category>/videos/*.mp4 in Google Drive and inserts new
videos into the database with status='new'. Downloads each file to
VIDEO_DOWNLOAD_DIR/<google_drive_file_id>.mp4.

Prerequisites
-------------
1. Python 3.10+
2. Install dependencies:
       pip install -r scripts/requirements.txt
3. Google service account JSON key with drive.readonly scope.
   - For local dev: save it to .secrets/google-drive.json (gitignored),
     then export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/.secrets/google-drive.json"
   - For production (VPS): see docs/setup/credentials.md
4. Set SUPABASE_DB_URL to a Postgres connection string
   (or pass --db-url on the command line).
5. Optionally set VIDEO_DOWNLOAD_DIR for where to store downloaded mp4s
   (defaults to ./.artifacts/videos).

See also: docs/contracts/environment.md for the full env var contract.

Usage
-----
    python scripts/sync_drive_videos.py [--dry-run] [--skip-download]
    python scripts/sync_drive_videos.py --db-url 'postgresql://...'
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DEFAULT_VIDEO_DIR = "./.artifacts/videos"
FOLDER_MIME = "application/vnd.google-apps.folder"
MP4_MIME = "video/mp4"

log = logging.getLogger("sync_drive_videos")


def validate_credentials() -> None:
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        print(
            "error: GOOGLE_APPLICATION_CREDENTIALS is not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not os.access(path, os.R_OK):
        print(
            f"error: credential file is missing or unreadable: {path[:32]}...",
            file=sys.stderr,
        )
        sys.exit(1)


def build_drive_service():
    import google.auth
    from googleapiclient.discovery import build

    credentials, _ = google.auth.default(scopes=[DRIVE_SCOPE])
    return build("drive", "v3", credentials=credentials)


def _list_children(service, parent_id: str, *, mime_type: str | None = None, name: str | None = None) -> list[dict]:
    q_parts = [f"'{parent_id}' in parents", "trashed=false"]
    if mime_type:
        q_parts.append(f"mimeType='{mime_type}'")
    if name:
        q_parts.append(f"name='{name}'")
    q = " and ".join(q_parts)

    items: list[dict] = []
    page_token = None
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken,files(id,name,parents,size,mimeType)",
            pageSize=100,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def discover_videos(service) -> list[dict]:
    """Walk instagram/<category>/videos/ and return video metadata dicts."""
    instagram_folders = service.files().list(
        q=f"name='instagram' and mimeType='{FOLDER_MIME}' and trashed=false",
        fields="files(id,name)",
        pageSize=100,
    ).execute().get("files", [])

    if not instagram_folders:
        log.warning("no 'instagram' folders found in Drive")
        return []

    videos: list[dict] = []
    for ig in instagram_folders:
        categories = _list_children(service, ig["id"], mime_type=FOLDER_MIME)
        for cat in categories:
            vid_folders = _list_children(
                service, cat["id"], mime_type=FOLDER_MIME, name="videos",
            )
            for vf in vid_folders:
                mp4s = _list_children(service, vf["id"], mime_type=MP4_MIME)
                source_path = f"instagram/{cat['name']}/videos/"
                for f in mp4s:
                    fname = f["name"]
                    ext = fname.rsplit(".", 1)[-1] if "." in fname else "mp4"
                    videos.append({
                        "google_drive_file_id": f["id"],
                        "google_drive_folder_id": vf["id"],
                        "source_path": source_path,
                        "filename": fname,
                        "extension": ext,
                        "mime_type": f.get("mimeType", MP4_MIME),
                        "size_bytes": int(f["size"]) if f.get("size") else None,
                        "category": cat["name"],
                        "platform": "instagram",
                        "download_method": "google_drive_api",
                    })
    return videos


def download_file(service, file_id: str, dest: pathlib.Path) -> None:
    from googleapiclient.http import MediaIoBaseDownload

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        request = service.files().get_media(fileId=file_id)
        with open(tmp, "wb") as fh:
            dl = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def fetch_existing_ids(db_url: str) -> set[str]:
    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT google_drive_file_id FROM automation.videos "
                "WHERE google_drive_file_id IS NOT NULL"
            )
            return {row[0] for row in cur.fetchall()}


INSERT_SQL = """\
INSERT INTO automation.videos
    (google_drive_file_id, google_drive_folder_id, source_path,
     filename, extension, mime_type, size_bytes, category,
     platform, download_method, local_video_path, status)
VALUES
    (%(google_drive_file_id)s, %(google_drive_folder_id)s,
     %(source_path)s, %(filename)s, %(extension)s,
     %(mime_type)s, %(size_bytes)s, %(category)s,
     %(platform)s, %(download_method)s, %(local_video_path)s, 'new')
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync video files from Google Drive into automation.videos.",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("SUPABASE_DB_URL"),
        help="Postgres connection string. Defaults to env SUPABASE_DB_URL.",
    )
    parser.add_argument(
        "--video-dir",
        default=os.environ.get("VIDEO_DOWNLOAD_DIR", DEFAULT_VIDEO_DIR),
        help="Where to store downloaded videos. Defaults to VIDEO_DOWNLOAD_DIR or ./.artifacts/videos.",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Register metadata only; do not download video files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and report without writing to the database or downloading.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    validate_credentials()

    if not args.db_url:
        print(
            "error: SUPABASE_DB_URL is not set and --db-url was not provided.",
            file=sys.stderr,
        )
        return 2

    service = build_drive_service()

    log.info("scanning Google Drive for videos...")
    discovered = discover_videos(service)
    log.info("discovered %d video(s) in Drive", len(discovered))

    if not discovered:
        print("0 video(s) in Drive, nothing to do.")
        return 0

    existing_ids = fetch_existing_ids(args.db_url)
    new_videos = [v for v in discovered if v["google_drive_file_id"] not in existing_ids]
    log.info(
        "%d new, %d already ingested",
        len(new_videos),
        len(discovered) - len(new_videos),
    )

    if not new_videos:
        print(f"0 new video(s) ({len(discovered)} already ingested).")
        return 0

    video_dir = pathlib.Path(args.video_dir).resolve()

    import psycopg

    conn = psycopg.connect(args.db_url)
    ingested = 0
    errors = 0

    try:
        for v in new_videos:
            file_id = v["google_drive_file_id"]
            local_path: str | None = None

            if not args.skip_download:
                dest = video_dir / f"{file_id}.mp4"
                if dest.exists():
                    local_path = str(dest)
                else:
                    try:
                        log.info("downloading %s → %s", v["filename"], dest.name)
                        if not args.dry_run:
                            download_file(service, file_id, dest)
                        local_path = str(dest)
                    except Exception as e:
                        log.error("download failed for %s: %s", v["filename"], e)
                        errors += 1
                        continue

            v["local_video_path"] = local_path

            if args.dry_run:
                print(
                    f"  would insert: {v['filename']}  "
                    f"category={v['category']}  id={file_id}"
                )
            else:
                with conn.cursor() as cur:
                    cur.execute(INSERT_SQL, v)
                conn.commit()
                log.info("inserted: %s (category=%s)", v["filename"], v["category"])
            ingested += 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    prefix = "DRY RUN: " if args.dry_run else ""
    print(
        f"{prefix}{ingested} video(s) ingested, {errors} error(s), "
        f"{len(discovered)} total in Drive."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
