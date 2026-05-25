"""Tests for video preparation tools."""

from pathlib import Path
from unittest.mock import patch

from src.worker.tools.video import _is_android_friendly, prepare_video_for_android


class TestIsAndroidFriendly:
    def test_h264_yuv420p_8bit(self):
        meta = {"codec_name": "h264", "pix_fmt": "yuv420p", "bits_per_raw_sample": "8"}
        assert _is_android_friendly(meta) is True

    def test_hevc_fails(self):
        meta = {"codec_name": "hevc", "pix_fmt": "yuv420p", "bits_per_raw_sample": "8"}
        assert _is_android_friendly(meta) is False

    def test_10bit_fails(self):
        meta = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p10le",
            "bits_per_raw_sample": "10",
        }
        assert _is_android_friendly(meta) is False

    def test_yuv444_fails(self):
        meta = {"codec_name": "h264", "pix_fmt": "yuv444p", "bits_per_raw_sample": "8"}
        assert _is_android_friendly(meta) is False

    def test_missing_bps_ok(self):
        meta = {"codec_name": "h264", "pix_fmt": "yuv420p"}
        assert _is_android_friendly(meta) is True

    def test_empty_meta(self):
        assert _is_android_friendly({}) is False

    def test_h264_no_pix_fmt_ok(self):
        meta = {"codec_name": "h264", "pix_fmt": "", "bits_per_raw_sample": "8"}
        assert _is_android_friendly(meta) is True


class TestPrepareVideoForAndroid:
    def test_file_not_found(self):
        result = prepare_video_for_android("/nonexistent/video.mp4")
        assert not result.success
        assert "not found" in result.message

    @patch("src.worker.tools.video._ffprobe_video_meta")
    def test_already_friendly(self, mock_probe):
        mock_probe.return_value = {
            "codec_name": "h264",
            "pix_fmt": "yuv420p",
            "bits_per_raw_sample": "8",
        }
        with patch.object(Path, "is_file", return_value=True):
            result = prepare_video_for_android("/tmp/test.mp4")
        assert result.success
        assert "already android-friendly" in result.message

    @patch("src.worker.tools.video._ffprobe_video_meta")
    def test_probe_error(self, mock_probe):
        mock_probe.side_effect = RuntimeError("ffprobe failed: corrupt")
        with patch.object(Path, "is_file", return_value=True):
            result = prepare_video_for_android("/tmp/test.mp4")
        assert not result.success
        assert "ffprobe error" in result.message
