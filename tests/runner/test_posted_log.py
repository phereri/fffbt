"""Tests for the append-only posted-reels JSONL log."""

from __future__ import annotations

import json

from src.runner.posted_log import PostedRecord, append_record, read_records


class TestAppendRead:
    def test_append_then_read_roundtrip(self, tmp_path):
        log = tmp_path / "posted.jsonl"
        append_record(
            PostedRecord.now(
                status="published", video_id="Cowboy", category="trend",
                source_key="ferma/Cowboy/a.mp4",
                post_url="https://www.instagram.com/reel/X/", device="d1",
                account="acc", caption="hi", verified=True,
            ),
            log,
        )
        recs = read_records(log)
        assert len(recs) == 1
        assert recs[0]["video_id"] == "Cowboy"
        assert recs[0]["platform"] == "instagram"
        assert recs[0]["post_url"].endswith("/reel/X/")

    def test_appends_multiple_lines(self, tmp_path):
        log = tmp_path / "posted.jsonl"
        for i in range(3):
            append_record(PostedRecord.now(status="published", video_id=f"v{i}"), log)
        recs = read_records(log)
        assert [r["video_id"] for r in recs] == ["v0", "v1", "v2"]
        # each line is independently valid JSON
        for line in log.read_text(encoding="utf-8").splitlines():
            json.loads(line)

    def test_creates_parent_dirs(self, tmp_path):
        log = tmp_path / "nested" / "dir" / "posted.jsonl"
        append_record(PostedRecord.now(status="published", video_id="v"), log)
        assert log.exists()

    def test_read_missing_returns_empty(self, tmp_path):
        assert read_records(tmp_path / "nope.jsonl") == []

    def test_read_skips_corrupt_lines(self, tmp_path):
        log = tmp_path / "posted.jsonl"
        log.write_text(
            '{"video_id": "ok"}\nnot json{\n{"video_id": "ok2"}\n',
            encoding="utf-8",
        )
        recs = read_records(log)
        assert [r["video_id"] for r in recs] == ["ok", "ok2"]

    def test_unicode_caption_preserved(self, tmp_path):
        log = tmp_path / "posted.jsonl"
        append_record(
            PostedRecord.now(status="published", caption="привет 😭 #гол"), log
        )
        assert read_records(log)[0]["caption"] == "привет 😭 #гол"


class TestEnvPath:
    def test_env_var_path(self, tmp_path, monkeypatch):
        target = tmp_path / "from_env.jsonl"
        monkeypatch.setenv("POSTED_REELS_LOG", str(target))
        append_record(PostedRecord.now(status="published", video_id="v"))
        assert target.exists()
        assert read_records()[0]["video_id"] == "v"


class TestRecordShape:
    def test_now_sets_ts_and_platform(self):
        r = PostedRecord.now(status="published")
        assert r.platform == "instagram"
        assert r.ts.endswith("Z")

    def test_empty_extra_dropped_from_line(self, tmp_path):
        log = tmp_path / "p.jsonl"
        append_record(PostedRecord.now(status="published"), log)
        line = log.read_text(encoding="utf-8").strip()
        assert "extra" not in line
