"""Tests for the standalone post_one orchestration (no real device / LLM)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.runner.post_one import PostOneResult, post_one
from src.worker.session.types import StepName, StepResult, StepStatus

_PREP = "src.runner.post_one.VideoPreparationStep"
_UI = "src.runner.post_one.MobileUIAutomationStep"
_VERIFY = "src.runner.post_one._verify_dashboard"
_CAPTURE = "src.runner.post_one._capture_post_url"


@pytest.fixture(autouse=True)
def _isolate_side_effects(tmp_path, monkeypatch):
    """Keep every test off the real device/LLM and off the repo's log file.

    - URL capture is patched to None by default (tests that care override it).
    - The posted-reels log is redirected to a tmp file so no test writes to the
      repo's posted_reels.jsonl.
    """
    monkeypatch.setenv("POSTED_REELS_LOG", str(tmp_path / "posted.jsonl"))
    with patch(_CAPTURE, new_callable=AsyncMock, return_value=None):
        yield


def run(coro):
    return asyncio.run(coro)


def _ok(step: StepName) -> StepResult:
    return StepResult(step=step, status=StepStatus.OK, message="ok")


def _fail(step: StepName, code: str, msg: str) -> StepResult:
    return StepResult(step=step, status=StepStatus.FAILED, code=code, message=msg)


def _needs_review(step: StepName, code: str, msg: str) -> StepResult:
    return StepResult(step=step, status=StepStatus.NEEDS_REVIEW, code=code, message=msg)


def _patch_prep(result: StepResult):
    p = patch(_PREP)
    mock_cls = p.start()
    mock_cls.return_value.run = AsyncMock(return_value=result)
    return p, mock_cls


def _patch_ui(result: StepResult):
    p = patch(_UI)
    mock_cls = p.start()
    mock_cls.return_value.run = AsyncMock(return_value=result)
    return p, mock_cls


class TestInputValidation:
    def test_no_device(self):
        r = run(post_one(device_serial="", video="v.mp4", caption="c"))
        assert not r.success and r.code == "INFRA"

    def test_no_caption(self):
        r = run(post_one(device_serial="d1", video="v.mp4", caption=""))
        assert not r.success and r.code == "INFRA"

    def test_no_video(self):
        r = run(post_one(device_serial="d1", video="", caption="c"))
        assert not r.success and r.code == "INFRA"


class TestPrepFailureShortCircuits:
    def test_prep_failure_skips_publish(self):
        p_prep, _ = _patch_prep(
            _fail(StepName.VIDEO_PREPARATION, "INFRA", "no such file")
        )
        p_ui, ui_cls = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        try:
            r = run(post_one(device_serial="d1", video="v.mp4", caption="c"))
        finally:
            p_prep.stop()
            p_ui.stop()
        assert not r.success
        assert not r.published
        assert r.verified is None
        assert "video_preparation failed" in r.message
        ui_cls.return_value.run.assert_not_called()  # never tried to publish


class TestPublishFailure:
    def test_publish_failure_skips_verify(self):
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(
            _needs_review(
                StepName.MOBILE_UI_AUTOMATION, "share_did_not_register", "no share"
            )
        )
        with patch(_VERIFY, new_callable=AsyncMock) as mock_verify:
            try:
                r = run(post_one(device_serial="d1", video="v.mp4", caption="c"))
            finally:
                p_prep.stop()
                p_ui.stop()
        assert not r.success
        assert not r.published
        assert r.verified is None
        mock_verify.assert_not_called()


class TestNoVerify:
    def test_publish_ok_no_verify(self):
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        with patch(_VERIFY, new_callable=AsyncMock) as mock_verify:
            try:
                r = run(
                    post_one(
                        device_serial="d1", video="v.mp4", caption="c", verify=False
                    )
                )
            finally:
                p_prep.stop()
                p_ui.stop()
        assert r.success
        assert r.published
        assert r.verified is None
        assert "skipped" in r.message
        mock_verify.assert_not_called()


class TestVerify:
    def test_publish_ok_verify_ok(self):
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        with patch(_VERIFY, new_callable=AsyncMock, return_value=True):
            try:
                r = run(post_one(device_serial="d1", video="v.mp4", caption="c"))
            finally:
                p_prep.stop()
                p_ui.stop()
        assert r.success
        assert r.published
        assert r.verified is True
        assert "dashboard" in r.message.lower()

    def test_publish_ok_verify_fail(self):
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        with patch(_VERIFY, new_callable=AsyncMock, return_value=False):
            try:
                r = run(post_one(device_serial="d1", video="v.mp4", caption="c"))
            finally:
                p_prep.stop()
                p_ui.stop()
        assert not r.success
        assert r.published
        assert r.verified is False
        assert r.code == "verification_failed"


class TestVideoSourceRouting:
    def _capture_prep_kwargs(self, video: str) -> dict:
        captured: dict = {}

        async def fake_run(ctx, **kwargs):
            captured.update(kwargs)
            return _ok(StepName.VIDEO_PREPARATION)

        p_prep = patch(_PREP)
        mock_cls = p_prep.start()
        mock_cls.return_value.run = fake_run
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        try:
            run(
                post_one(
                    device_serial="d1", video=video, caption="c", verify=False
                )
            )
        finally:
            p_prep.stop()
            p_ui.stop()
        return captured

    def test_url_routes_to_video_url(self):
        kw = self._capture_prep_kwargs("https://bucket.s3.amazonaws.com/x.mp4?sig=1")
        assert "video_url" in kw
        assert "local_video_path" not in kw

    def test_local_path_routes_to_local_video_path(self):
        kw = self._capture_prep_kwargs("/tmp/clip.mp4")
        assert "local_video_path" in kw
        assert "video_url" not in kw


class TestUrlCapture:
    def test_captured_url_on_result(self):
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        with patch(_VERIFY, new_callable=AsyncMock, return_value=True), patch(
            _CAPTURE,
            new_callable=AsyncMock,
            return_value="https://www.instagram.com/reel/ABC/",
        ):
            try:
                r = run(post_one(device_serial="d1", video="v.mp4", caption="c"))
            finally:
                p_prep.stop()
                p_ui.stop()
        assert r.post_url == "https://www.instagram.com/reel/ABC/"
        assert "instagram.com" in r.message

    def test_no_capture_when_disabled(self):
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        with patch(_VERIFY, new_callable=AsyncMock, return_value=True), patch(
            _CAPTURE, new_callable=AsyncMock
        ) as cap:
            try:
                r = run(
                    post_one(
                        device_serial="d1", video="v.mp4", caption="c",
                        capture_url=False,
                    )
                )
            finally:
                p_prep.stop()
                p_ui.stop()
        assert r.post_url is None
        cap.assert_not_called()


class TestPostedLog:
    def _post(self, tmp_path, **kw):
        from src.runner import posted_log

        log = tmp_path / "posted.jsonl"
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(_ok(StepName.MOBILE_UI_AUTOMATION))
        with patch(_VERIFY, new_callable=AsyncMock, return_value=True):
            try:
                run(
                    post_one(
                        device_serial="d1", video="v.mp4", caption="c",
                        log_path=str(log), **kw,
                    )
                )
            finally:
                p_prep.stop()
                p_ui.stop()
        return posted_log.read_records(log)

    def test_logs_provenance_on_success(self, tmp_path):
        recs = self._post(
            tmp_path,
            bucket_video_id="Cowboy",
            category="trend",
            source_key="ferma/Cowboy/VID_x.mp4",
        )
        assert len(recs) == 1
        r = recs[0]
        assert r["video_id"] == "Cowboy"
        assert r["category"] == "trend"
        assert r["source_key"] == "ferma/Cowboy/VID_x.mp4"
        assert r["status"] == "published"
        assert r["platform"] == "instagram"
        assert r["verified"] is True
        assert "ts" in r

    def test_logs_failed_publish(self, tmp_path):
        from src.runner import posted_log

        log = tmp_path / "p.jsonl"
        p_prep, _ = _patch_prep(_ok(StepName.VIDEO_PREPARATION))
        p_ui, _ = _patch_ui(
            _needs_review(StepName.MOBILE_UI_AUTOMATION, "share_did_not_register", "x")
        )
        try:
            run(
                post_one(
                    device_serial="d1", video="v.mp4", caption="c",
                    bucket_video_id="Cowboy", log_path=str(log),
                )
            )
        finally:
            p_prep.stop()
            p_ui.stop()
        recs = posted_log.read_records(log)
        assert len(recs) == 1
        assert recs[0]["status"] == "failed"
        assert recs[0]["code"] == "share_did_not_register"

    def test_no_log_when_prep_fails(self, tmp_path):
        from src.runner import posted_log

        log = tmp_path / "p.jsonl"
        p_prep, _ = _patch_prep(_fail(StepName.VIDEO_PREPARATION, "INFRA", "x"))
        try:
            run(
                post_one(
                    device_serial="d1", video="v.mp4", caption="c", log_path=str(log)
                )
            )
        finally:
            p_prep.stop()
        # prep failure happens before publish; nothing posted -> no log line
        assert posted_log.read_records(log) == []


class TestResultType:
    def test_returns_dataclass(self):
        r = run(post_one(device_serial="", video="v", caption="c"))
        assert isinstance(r, PostOneResult)
