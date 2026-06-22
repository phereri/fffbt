"""Tests for the S3 -> fffbt.videos sync (no boto3, no DB — all injected)."""

from __future__ import annotations

import pytest

from src.runner.s3_source import FolderMeta, S3Config, VideoFolder
from src.runner import s3_sync


def _folder(video_id, keys, *, platform=None, category=None, caption=None, meta=True):
    m = None
    if meta:
        m = FolderMeta(platform=platform or [], category=category, caption=caption)
    return VideoFolder(
        video_id=video_id,
        prefix=f"ferma/{video_id}/",
        video_keys=[f"ferma/{video_id}/{k}" for k in keys],
        meta=m,
    )


def _ids():
    """Deterministic id factory so assertions are stable."""
    seq = iter(f"id{n:02d}" for n in range(100))
    return lambda: next(seq)


class TestBuildCandidates:
    def test_one_row_per_video_times_platform(self):
        folders = [
            _folder("Gussi", ["a.mp4", "b.mp4"],
                    platform=["Instagram", "Tiktok"], category="trend", caption="hi")
        ]
        rows, skipped = s3_sync.build_candidates(folders, "neiroslop", id_factory=_ids())
        assert skipped == []
        # 2 files x 2 platforms = 4 rows
        assert len(rows) == 4
        platforms = sorted(r["platform"] for r in rows)
        assert platforms == ["Instagram", "Instagram", "Tiktok", "Tiktok"]

    def test_row_mapping(self):
        folders = [_folder("Gussi", ["VID_1.mp4"],
                           platform=["Instagram"], category="trend", caption="cap")]
        rows, _ = s3_sync.build_candidates(folders, "neiroslop", id_factory=_ids())
        r = rows[0]
        assert r["name"] == "VID_1.mp4"
        assert r["link_drive"] == "s3://neiroslop/ferma/Gussi/VID_1.mp4"
        assert r["platform"] == "Instagram"
        assert r["category"] == "trend"
        assert r["caption"] == "cap"
        assert r["status"] == "new"
        assert r["type"] == ""
        assert r["id"] == "id00"

    def test_missing_caption_is_none(self):
        folders = [_folder("X", ["a.mp4"], platform=["Instagram"], category="trend")]
        rows, _ = s3_sync.build_candidates(folders, "b", id_factory=_ids())
        assert rows[0]["caption"] is None

    @pytest.mark.parametrize("folder", [
        _folder("nometa", ["a.mp4"], meta=False),
        _folder("nocat", ["a.mp4"], platform=["Instagram"], category=None),
        _folder("noplat", ["a.mp4"], platform=[], category="trend"),
    ])
    def test_unusable_folders_skipped(self, folder):
        rows, skipped = s3_sync.build_candidates([folder], "b", id_factory=_ids())
        assert rows == []
        assert skipped == [folder.video_id]


class TestInsertSql:
    def test_columns_and_values(self):
        rows = [{
            "id": "id00", "name": "v.mp4", "platform": "Instagram",
            "category": "trend", "type": "", "status": "new",
            "link_drive": "s3://b/ferma/X/v.mp4", "caption": None,
        }]
        sql = s3_sync.insert_sql(rows)
        assert sql.startswith(
            "INSERT INTO fffbt.videos (id, name, platform, category, type, status, link_drive, caption) VALUES"
        )
        assert "'id00'" in sql and "'s3://b/ferma/X/v.mp4'" in sql
        assert "NULL" in sql  # caption

    def test_escapes_single_quotes(self):
        rows = [{
            "id": "x", "name": "v.mp4", "platform": "Instagram", "category": "trend",
            "type": "", "status": "new", "link_drive": "s3://b/k",
            "caption": "it's a 'test'",
        }]
        sql = s3_sync.insert_sql(rows)
        assert "it''s a ''test''" in sql


class _FakeS3:
    def __init__(self, folders):
        self._folders = {f.video_id: f for f in folders}
        self.config = S3Config(
            endpoint="x", region="ru-1", bucket="neiroslop", prefix="ferma/",
            access_key="a", secret_key="s",
        )

    def list_folders(self):
        return list(self._folders)

    def get_folder(self, video_id):
        return self._folders[video_id]


class TestSyncOnce:
    def test_inserts_only_new_pairs(self):
        s3 = _FakeS3([
            _folder("Gussi", ["a.mp4", "b.mp4"], platform=["Instagram"], category="trend"),
        ])
        # a.mp4 already in DB -> only b.mp4 is new
        existing = {("s3://neiroslop/ferma/Gussi/a.mp4", "Instagram")}
        captured: list[dict] = []

        def insert(rows):
            captured.extend(rows)
            return len(rows)

        res = s3_sync.sync_once(
            s3=s3, fetch_existing=lambda: existing, insert=insert, id_factory=_ids()
        )
        assert res.inserted == 1
        assert res.skipped == 1
        assert res.candidates == 2
        assert res.folders == 1
        assert [r["link_drive"] for r in captured] == ["s3://neiroslop/ferma/Gussi/b.mp4"]

    def test_dedups_within_a_single_pass(self):
        # A folder that lists the same platform twice must not double-insert.
        s3 = _FakeS3([_folder("X", ["a.mp4"],
                              platform=["Instagram", "Instagram"], category="trend")])
        captured: list[dict] = []
        res = s3_sync.sync_once(
            s3=s3, fetch_existing=lambda: set(),
            insert=lambda rows: captured.extend(rows) or len(rows), id_factory=_ids(),
        )
        assert res.inserted == 1
        assert len(captured) == 1

    def test_folder_without_meta_is_reported_not_inserted(self):
        s3 = _FakeS3([_folder("nometa", ["a.mp4"], meta=False)])
        res = s3_sync.sync_once(
            s3=s3, fetch_existing=lambda: set(), insert=lambda rows: len(rows),
            id_factory=_ids(),
        )
        assert res.inserted == 0
        assert res.folders_skipped == 1
        assert res.skipped_folder_ids == ["nometa"]

    def test_deletion_in_s3_never_removes_db_rows(self):
        # The bucket is empty but the DB has a row — sync must touch nothing.
        s3 = _FakeS3([])
        deleted_calls = []
        res = s3_sync.sync_once(
            s3=s3,
            fetch_existing=lambda: {("s3://neiroslop/ferma/Old/v.mp4", "Instagram")},
            insert=lambda rows: deleted_calls.append(rows) or len(rows),
            id_factory=_ids(),
        )
        assert res.inserted == 0
        # insert was called only with an empty list; no delete path exists at all
        assert deleted_calls == [[]]

    def test_age_gate_drops_old_and_undated(self):
        from datetime import date
        s3 = _FakeS3([_folder(
            "Mix",
            ["VID_20260620_a.mp4",   # 3 days old -> kept
             "VID_20260601_b.mp4",   # 22 days old -> dropped
             "nodate_c.mp4"],        # no date -> dropped
            platform=["Instagram"], category="trend",
        )])
        captured: list[dict] = []
        res = s3_sync.sync_once(
            s3=s3, fetch_existing=lambda: set(),
            insert=lambda rows: captured.extend(rows) or len(rows),
            id_factory=_ids(), max_age_days=7, today=date(2026, 6, 23),
        )
        assert res.inserted == 1
        assert res.skipped_old == 2
        assert [r["name"] for r in captured] == ["VID_20260620_a.mp4"]

    def test_age_gate_off_by_default_keeps_undated(self):
        # Without max_age_days every candidate (even undated) is inserted.
        s3 = _FakeS3([_folder("X", ["nodate.mp4"], platform=["Instagram"], category="trend")])
        res = s3_sync.sync_once(
            s3=s3, fetch_existing=lambda: set(),
            insert=lambda rows: len(rows), id_factory=_ids(),
        )
        assert res.inserted == 1
        assert res.skipped_old == 0
