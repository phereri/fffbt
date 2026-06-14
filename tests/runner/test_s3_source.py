"""Tests for the S3 video source (mocked boto3 — no network)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.runner.s3_source import FermaS3, FolderMeta, S3Config


def _config() -> S3Config:
    return S3Config(
        endpoint="https://s3.example",
        region="ru-1",
        bucket="b",
        prefix="ferma/",
        access_key="AK",
        secret_key="SK",
    )


def _client_with_pages(pages_by_op: dict) -> MagicMock:
    """Build a fake boto3 client whose paginator yields the given pages."""
    client = MagicMock()

    def get_paginator(op):
        pag = MagicMock()
        pag.paginate = MagicMock(return_value=iter(pages_by_op.get(op, [])))
        return pag

    client.get_paginator.side_effect = get_paginator
    return client


class TestS3Config:
    def test_from_env_defaults(self):
        cfg = S3Config.from_env({})
        assert cfg.endpoint == "https://s3.twcstorage.ru"
        assert cfg.bucket == "neiroslop"
        assert cfg.prefix == "ferma/"

    def test_from_env_overrides_and_prefix_slash(self):
        cfg = S3Config.from_env(
            {
                "FERMA_S3_ENDPOINT": "https://x",
                "FERMA_S3_BUCKET": "mybucket",
                "FERMA_S3_PREFIX": "root",  # no trailing slash
                "FERMA_S3_ACCESS_KEY": "a",
                "FERMA_S3_SECRET_KEY": "s",
            }
        )
        assert cfg.bucket == "mybucket"
        assert cfg.prefix == "root/"  # normalized
        assert cfg.access_key == "a"


class TestFolderMeta:
    def test_from_dict_full(self):
        m = FolderMeta.from_dict(
            {"platform": ["Tiktok", "Instagram"], "category": "trend", "caption": "hi"}
        )
        assert m.platform == ["Tiktok", "Instagram"]
        assert m.category == "trend"
        assert m.caption == "hi"
        assert m.raw["category"] == "trend"

    def test_platform_string_coerced_to_list(self):
        m = FolderMeta.from_dict({"platform": "Instagram", "category": "mems"})
        assert m.platform == ["Instagram"]

    def test_missing_fields(self):
        m = FolderMeta.from_dict({})
        assert m.platform == []
        assert m.category is None
        assert m.caption is None


class TestListFolders:
    def test_lists_video_id_names(self):
        pages = {
            "list_objects_v2": [
                {
                    "CommonPrefixes": [
                        {"Prefix": "ferma/Cowboy/"},
                        {"Prefix": "ferma/MrBeast/"},
                    ]
                },
                {"CommonPrefixes": [{"Prefix": "ferma/24часа/"}]},
            ]
        }
        s3 = FermaS3(_config(), client=_client_with_pages(pages))
        assert s3.list_folders() == ["Cowboy", "MrBeast", "24часа"]


class TestListVideos:
    def test_filters_to_video_suffixes_and_sorts(self):
        pages = {
            "list_objects_v2": [
                {
                    "Contents": [
                        {"Key": "ferma/F/b.mp4"},
                        {"Key": "ferma/F/a.mp4"},
                        {"Key": "ferma/F/meta.json"},
                        {"Key": "ferma/F/note.txt"},
                        {"Key": "ferma/F/c.MOV"},
                    ]
                }
            ]
        }
        s3 = FermaS3(_config(), client=_client_with_pages(pages))
        assert s3.list_videos("F") == [
            "ferma/F/a.mp4",
            "ferma/F/b.mp4",
            "ferma/F/c.MOV",
        ]


class TestReadMeta:
    def test_parses_meta_json(self):
        client = MagicMock()
        body = MagicMock()
        body.read.return_value = json.dumps(
            {"platform": ["Instagram"], "category": "mems", "caption": "c"}
        ).encode()
        client.get_object.return_value = {"Body": body}
        s3 = FermaS3(_config(), client=client)
        meta = s3.read_meta("F")
        assert meta is not None
        assert meta.category == "mems"
        client.get_object.assert_called_once_with(Bucket="b", Key="ferma/F/meta.json")

    def test_missing_returns_none(self):
        client = MagicMock()
        client.get_object.side_effect = Exception("NoSuchKey")
        s3 = FermaS3(_config(), client=client)
        assert s3.read_meta("F") is None

    def test_bad_json_returns_none(self):
        client = MagicMock()
        body = MagicMock()
        body.read.return_value = b"not json{"
        client.get_object.return_value = {"Body": body}
        s3 = FermaS3(_config(), client=client)
        assert s3.read_meta("F") is None


class TestDownloadDelete:
    def test_download_creates_parent(self, tmp_path):
        client = MagicMock()
        s3 = FermaS3(_config(), client=client)
        dest = tmp_path / "sub" / "x.mp4"
        out = s3.download("ferma/F/x.mp4", dest)
        assert out == dest
        assert dest.parent.is_dir()
        client.download_file.assert_called_once_with("b", "ferma/F/x.mp4", str(dest))

    def test_delete_object(self):
        client = MagicMock()
        s3 = FermaS3(_config(), client=client)
        s3.delete("ferma/F/x.mp4")
        client.delete_object.assert_called_once_with(Bucket="b", Key="ferma/F/x.mp4")

    def test_delete_folder_counts(self):
        pages = {
            "list_objects_v2": [
                {"Contents": [{"Key": "ferma/F/a.mp4"}, {"Key": "ferma/F/b.mp4"}]},
                {"Contents": [{"Key": "ferma/F/meta.json"}]},
            ]
        }
        client = _client_with_pages(pages)
        s3 = FermaS3(_config(), client=client)
        n = s3.delete_folder("F")
        assert n == 3
        client.delete_objects.assert_called_once()


class TestCredentialGuard:
    def test_missing_creds_raises_on_build(self):
        cfg = S3Config(
            endpoint="e", region="r", bucket="b", prefix="ferma/",
            access_key="", secret_key="",
        )
        s3 = FermaS3(cfg)  # no injected client -> will try to build
        with pytest.raises(RuntimeError, match="credentials"):
            _ = s3.client
