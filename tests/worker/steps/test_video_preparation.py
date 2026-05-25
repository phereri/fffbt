"""Tests for the video_preparation step."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.worker.session.types import Mode, StepContext, StepName, StepStatus
from src.worker.steps.video_preparation import VideoPreparationStep
from src.worker.tools._types import ToolResult


def _ctx(**overrides) -> StepContext:
    defaults = dict(
        job_id="j1",
        video_id="v1",
        account_id="a1",
        account_environment_id="ae1",
        device_id="d1",
        mode=Mode.MVP,
        settings={},
    )
    defaults.update(overrides)
    return StepContext(**defaults)


def run(coro):
    return asyncio.run(coro)


class TestNoInput:
    def test_no_url_or_path(self):
        step = VideoPreparationStep()
        result = run(step.run(_ctx(), device_serial="serial1"))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"
        assert "no video_url or local_video_path" in result.message


class TestValidation:
    def test_invalid_extension(self, tmp_path):
        bad = tmp_path / "clip.avi"
        bad.write_bytes(b"\x00" * 2048)
        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(bad), device_serial="s"))
        assert result.status == StepStatus.FAILED
        assert "invalid extension" in result.message

    def test_file_too_small(self, tmp_path):
        small = tmp_path / "tiny.mp4"
        small.write_bytes(b"\x00" * 10)
        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(small), device_serial="s"))
        assert result.status == StepStatus.FAILED
        assert "too small" in result.message

    def test_file_too_large(self, tmp_path):
        huge = tmp_path / "huge.mp4"
        huge.write_bytes(b"\x00" * 2048)
        step = VideoPreparationStep()
        with patch.object(
            Path, "stat", return_value=type("S", (), {"st_size": 600 * 1024 * 1024})()
        ):
            result = run(step.run(_ctx(), local_video_path=str(huge), device_serial="s"))
        assert result.status == StepStatus.FAILED
        assert "too large" in result.message

    def test_local_path_not_found(self):
        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path="/no/such/file.mp4", device_serial="s"))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"


class TestHappyPath:
    @patch("src.worker.steps.video_preparation.adb_shell", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.push_video_to_gallery", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.prepare_video_for_android")
    def test_local_file_ok(self, mock_transcode, mock_push, mock_shell, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 4096)

        mock_transcode.return_value = ToolResult.ok("video already android-friendly: clip.mp4")
        mock_push.return_value = ToolResult.ok("pushed and scanned /sdcard/DCIM/Camera/clip.mp4")
        mock_shell.return_value = "/sdcard/DCIM/Camera/clip.mp4"

        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(video), device_serial="serial1"))

        assert result.status == StepStatus.OK
        assert result.step == StepName.VIDEO_PREPARATION
        assert "/sdcard/DCIM/Camera/clip.mp4" in result.message
        mock_push.assert_awaited_once()

    @patch("src.worker.steps.video_preparation.adb_shell", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.push_video_to_gallery", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.prepare_video_for_android")
    def test_transcode_needed(self, mock_transcode, mock_push, mock_shell, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 4096)
        transcoded = tmp_path / "clip_h264_yuv420p.mp4"
        transcoded.write_bytes(b"\x00" * 4096)

        mock_transcode.return_value = ToolResult.ok(f"transcoded -> {transcoded}")
        mock_push.return_value = ToolResult.ok("pushed and scanned")
        mock_shell.return_value = f"/sdcard/DCIM/Camera/{transcoded.name}"

        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(video), device_serial="s"))

        assert result.status == StepStatus.OK
        mock_push.assert_awaited_once()
        call_args = mock_push.call_args
        assert "clip_h264_yuv420p.mp4" in call_args[0][1]


class TestPushRetry:
    @patch("src.worker.steps.video_preparation.adb_shell", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.push_video_to_gallery", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.prepare_video_for_android")
    def test_push_retries_then_succeeds(self, mock_transcode, mock_push, mock_shell, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 4096)

        mock_transcode.return_value = ToolResult.ok("already android-friendly")
        mock_push.side_effect = [
            ToolResult.fail("adb push: timeout"),
            ToolResult.ok("pushed and scanned /sdcard/DCIM/Camera/clip.mp4"),
        ]
        mock_shell.return_value = "/sdcard/DCIM/Camera/clip.mp4"

        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(video), device_serial="s"))

        assert result.status == StepStatus.OK
        assert mock_push.await_count == 2

    @patch("src.worker.steps.video_preparation.push_video_to_gallery", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.prepare_video_for_android")
    def test_push_retries_exhausted(self, mock_transcode, mock_push, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 4096)

        mock_transcode.return_value = ToolResult.ok("already android-friendly")
        mock_push.return_value = ToolResult.fail("adb push: device offline")

        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(video), device_serial="s"))

        assert result.status == StepStatus.FAILED
        assert result.code == "device_offline"
        assert result.retryable is True
        assert mock_push.await_count == 3  # initial + 2 retries


class TestVerification:
    @patch("src.worker.steps.video_preparation.adb_shell", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.push_video_to_gallery", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.prepare_video_for_android")
    def test_verify_fails(self, mock_transcode, mock_push, mock_shell, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 4096)

        mock_transcode.return_value = ToolResult.ok("already android-friendly")
        mock_push.return_value = ToolResult.ok("pushed")
        mock_shell.side_effect = RuntimeError("device unreachable")

        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(video), device_serial="s"))

        assert result.status == StepStatus.FAILED
        assert result.code == "device_offline"
        assert result.retryable is True


class TestStepResultContract:
    @patch("src.worker.steps.video_preparation.adb_shell", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.push_video_to_gallery", new_callable=AsyncMock)
    @patch("src.worker.steps.video_preparation.prepare_video_for_android")
    def test_result_shape(self, mock_transcode, mock_push, mock_shell, tmp_path):
        """Every result has the required StepResult fields."""
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"\x00" * 4096)

        mock_transcode.return_value = ToolResult.ok("already android-friendly")
        mock_push.return_value = ToolResult.ok("pushed")
        mock_shell.return_value = "/sdcard/DCIM/Camera/clip.mp4"

        step = VideoPreparationStep()
        result = run(step.run(_ctx(), local_video_path=str(video), device_serial="s"))

        assert result.step == StepName.VIDEO_PREPARATION
        assert result.status in StepStatus
        assert isinstance(result.message, str)
        assert isinstance(result.warnings, list)
        assert isinstance(result.artifacts, list)

    def test_failure_result_shape(self):
        step = VideoPreparationStep()
        result = run(step.run(_ctx(), device_serial="s"))

        assert result.step == StepName.VIDEO_PREPARATION
        assert result.status == StepStatus.FAILED
        assert result.code is not None
        assert isinstance(result.message, str)
        assert isinstance(result.warnings, list)
        assert isinstance(result.artifacts, list)
