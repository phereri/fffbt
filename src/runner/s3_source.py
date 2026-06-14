"""S3 video source for the standalone runner (TWC Storage, S3-compatible).

The bucket holds one folder per ``video_id`` under a shared prefix (default
``ferma/``). Each folder contains many ``VID_*.mp4`` files plus one ``meta.json``
that applies to the whole folder::

    ferma/<video_id>/uniq_video1.mp4
    ferma/<video_id>/uniq_video2.mp4
    ferma/<video_id>/meta.json   ->  {"platform": [...], "category": "...", "caption": "..."}

This module is a thin access layer only: list folders, read a folder's meta,
list its videos, download one video to a local path, and delete objects (used
once a video has been posted to every required platform). It contains no
category / scheduling / caption-uniquification logic — that lives elsewhere.

``boto3`` is imported lazily so the rest of the runner works without it.
Configuration comes from ``FERMA_S3_*`` environment variables (see
``config/runner.env.example``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://s3.twcstorage.ru"
_DEFAULT_REGION = "ru-1"
_DEFAULT_BUCKET = "neiroslop"
_DEFAULT_PREFIX = "ferma/"

_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v")


@dataclass(frozen=True)
class S3Config:
    """Connection settings for the Ferma S3 bucket."""

    endpoint: str
    region: str
    bucket: str
    prefix: str
    access_key: str
    secret_key: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "S3Config":
        e = env if env is not None else os.environ
        prefix = e.get("FERMA_S3_PREFIX", _DEFAULT_PREFIX)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return cls(
            endpoint=e.get("FERMA_S3_ENDPOINT", _DEFAULT_ENDPOINT),
            region=e.get("FERMA_S3_REGION", _DEFAULT_REGION),
            bucket=e.get("FERMA_S3_BUCKET", _DEFAULT_BUCKET),
            prefix=prefix,
            access_key=e.get("FERMA_S3_ACCESS_KEY", ""),
            secret_key=e.get("FERMA_S3_SECRET_KEY", ""),
        )


@dataclass
class FolderMeta:
    """Parsed ``meta.json`` for one ``video_id`` folder.

    Unknown / extra keys are preserved in ``raw`` so nothing is lost.
    """

    platform: list[str] = field(default_factory=list)
    category: str | None = None
    caption: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FolderMeta":
        platform = data.get("platform") or []
        if isinstance(platform, str):
            platform = [platform]
        return cls(
            platform=[str(p) for p in platform],
            category=(str(data["category"]) if data.get("category") else None),
            caption=(data.get("caption") if data.get("caption") is not None else None),
            raw=dict(data),
        )


@dataclass
class VideoFolder:
    """One ``video_id`` folder: its key prefix, videos, and meta."""

    video_id: str
    prefix: str  # full S3 prefix, e.g. "ferma/Cowboy/"
    video_keys: list[str] = field(default_factory=list)
    meta: FolderMeta | None = None


class FermaS3:
    """Thin client over the Ferma S3 bucket.

    Construct via ``FermaS3.from_env()`` (reads ``FERMA_S3_*``) or pass an
    explicit ``S3Config``. The underlying boto3 client is created lazily on
    first use so importing this module never requires boto3.
    """

    def __init__(self, config: S3Config, *, client: Any | None = None) -> None:
        self._config = config
        self._client = client

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FermaS3":
        return cls(S3Config.from_env(env))

    @property
    def config(self) -> S3Config:
        return self._config

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.client import Config as BotoConfig
        except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "boto3 is required for S3 access. Install it: "
                "pip install boto3 (or pip install -r requirements-dev.txt)."
            ) from exc

        cfg = self._config
        if not cfg.access_key or not cfg.secret_key:
            raise RuntimeError(
                "S3 credentials missing — set FERMA_S3_ACCESS_KEY and "
                "FERMA_S3_SECRET_KEY (see config/runner.env.example)."
            )
        return boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            region_name=cfg.region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    # -- listing -------------------------------------------------------------

    def list_folders(self) -> list[str]:
        """Return the ``video_id`` names directly under the configured prefix."""
        paginator = self.client.get_paginator("list_objects_v2")
        names: list[str] = []
        for page in paginator.paginate(
            Bucket=self._config.bucket,
            Prefix=self._config.prefix,
            Delimiter="/",
        ):
            for cp in page.get("CommonPrefixes", []):
                full = cp["Prefix"]  # e.g. "ferma/Cowboy/"
                name = full[len(self._config.prefix) :].rstrip("/")
                if name:
                    names.append(name)
        return names

    def list_videos(self, video_id: str) -> list[str]:
        """Return full S3 keys of the video files in one folder (sorted)."""
        prefix = self._folder_prefix(video_id)
        paginator = self.client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.lower().endswith(_VIDEO_SUFFIXES):
                    keys.append(key)
        return sorted(keys)

    def read_meta(self, video_id: str) -> FolderMeta | None:
        """Read and parse ``<video_id>/meta.json``; None if absent/unparseable."""
        key = self._folder_prefix(video_id) + "meta.json"
        try:
            body = self.client.get_object(Bucket=self._config.bucket, Key=key)[
                "Body"
            ].read()
        except Exception as e:
            logger.info("no meta.json for %s (%s)", video_id, type(e).__name__)
            return None
        try:
            return FolderMeta.from_dict(json.loads(body))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("meta.json for %s is not valid JSON: %s", video_id, e)
            return None

    def get_folder(self, video_id: str) -> VideoFolder:
        """List videos + read meta for one folder in a single call."""
        return VideoFolder(
            video_id=video_id,
            prefix=self._folder_prefix(video_id),
            video_keys=self.list_videos(video_id),
            meta=self.read_meta(video_id),
        )

    # -- transfer ------------------------------------------------------------

    def download(self, key: str, dest: str | Path) -> Path:
        """Download one object by full key to ``dest`` (parents created)."""
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self._config.bucket, key, str(dest_path))
        return dest_path

    def delete(self, key: str) -> None:
        """Delete one object by full key."""
        self.client.delete_object(Bucket=self._config.bucket, Key=key)

    def delete_folder(self, video_id: str) -> int:
        """Delete every object under a ``video_id`` folder. Returns count.

        Used once a folder's videos have been posted to all required platforms.
        """
        prefix = self._folder_prefix(video_id)
        paginator = self.client.get_paginator("list_objects_v2")
        to_delete: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                to_delete.append({"Key": obj["Key"]})
        deleted = 0
        for batch_start in range(0, len(to_delete), 1000):
            batch = to_delete[batch_start : batch_start + 1000]
            if not batch:
                continue
            self.client.delete_objects(
                Bucket=self._config.bucket, Delete={"Objects": batch}
            )
            deleted += len(batch)
        return deleted

    # -- helpers -------------------------------------------------------------

    def _folder_prefix(self, video_id: str) -> str:
        vid = video_id.strip("/")
        return f"{self._config.prefix}{vid}/"


__all__ = ["FermaS3", "S3Config", "FolderMeta", "VideoFolder"]
