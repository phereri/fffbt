"""Tests for the standalone runner CLI (argparse wiring, exit codes)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.runner import cli
from src.runner.post_one import PostOneResult

_POST_ONE = "src.runner.cli.post_one"


def _result(success: bool, **kw) -> PostOneResult:
    return PostOneResult(
        success=success,
        published=kw.get("published", success),
        verified=kw.get("verified"),
        message=kw.get("message", "msg"),
        code=kw.get("code"),
        details=kw.get("details", {}),
    )


class TestSplitHashtags:
    def test_none(self):
        assert cli._split_hashtags(None) == []

    def test_comma_and_space(self):
        assert cli._split_hashtags("a, b  c,d") == ["a", "b", "c", "d"]


class TestPostOneCommand:
    def test_success_exit_zero(self):
        # post_one is async; the CLI awaits it via _run_async, so the patch must
        # be a coroutine function.
        async def _coro(**kwargs):
            return _result(True)

        with patch(_POST_ONE, _coro):
            rc = cli.main(
                [
                    "post-one",
                    "--device",
                    "d1:5555",
                    "--video",
                    "/tmp/x.mp4",
                    "--caption",
                    "hello",
                ]
            )
        assert rc == 0

    def test_failure_exit_one(self):
        async def _coro(**kwargs):
            return _result(False, code="verification_failed")

        with patch(_POST_ONE, _coro):
            rc = cli.main(
                [
                    "post-one",
                    "--device",
                    "d1:5555",
                    "--video",
                    "/tmp/x.mp4",
                    "--caption",
                    "hello",
                ]
            )
        assert rc == 1

    def test_kwargs_forwarded(self):
        seen: dict = {}

        async def _coro(**kwargs):
            seen.update(kwargs)
            return _result(True)

        with patch(_POST_ONE, _coro):
            cli.main(
                [
                    "post-one",
                    "--device",
                    "d1:5555",
                    "--video",
                    "/tmp/x.mp4",
                    "--caption",
                    "hello",
                    "--hashtags",
                    "foo,bar",
                    "--no-verify",
                    "--verify-delay",
                    "60",
                ]
            )
        assert seen["device_serial"] == "d1:5555"
        assert seen["video"] == "/tmp/x.mp4"
        assert seen["caption"] == "hello"
        assert seen["hashtags"] == ["foo", "bar"]
        assert seen["verify"] is False
        assert seen["verify_delay_seconds"] == 60

    def test_missing_required_arg_errors(self):
        with pytest.raises(SystemExit):
            cli.main(["post-one", "--device", "d1"])  # no --video / --caption


class TestDevicesCommand:
    def test_lists_devices(self, capsys):
        fake = MagicMock()
        fake.stdout = "List of devices attached\n100.100.57.41:5555\tdevice\n"
        fake.stderr = ""
        with patch("src.runner.cli.subprocess.run", return_value=fake):
            rc = cli.main(["devices"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "100.100.57.41:5555" in out

    def test_connect_then_list(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            m = MagicMock()
            m.stdout = "connected\n" if "connect" in args else "List of devices\n"
            m.stderr = ""
            return m

        with patch("src.runner.cli.subprocess.run", side_effect=fake_run):
            rc = cli.main(["devices", "--connect", "1.2.3.4:5555"])
        assert rc == 0
        assert any("connect" in c for c in calls)


class TestS3Commands:
    def _fake_s3(self):
        from src.runner.s3_source import FolderMeta, VideoFolder

        s3 = MagicMock()
        s3.config.prefix = "ferma/"
        s3.list_folders.return_value = ["Cowboy", "MrBeast"]
        s3.read_meta.return_value = FolderMeta.from_dict(
            {"platform": ["Instagram"], "category": "trend", "caption": "hi"}
        )
        s3.get_folder.return_value = VideoFolder(
            video_id="Cowboy",
            prefix="ferma/Cowboy/",
            video_keys=["ferma/Cowboy/a.mp4", "ferma/Cowboy/b.mp4"],
            meta=s3.read_meta.return_value,
        )
        return s3

    def test_ls_folders(self, capsys):
        s3 = self._fake_s3()
        with patch("src.runner.cli._s3_client", return_value=s3):
            rc = cli.main(["s3", "ls"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Cowboy" in out and "MrBeast" in out

    def test_ls_folder_json(self, capsys):
        s3 = self._fake_s3()
        with patch("src.runner.cli._s3_client", return_value=s3):
            rc = cli.main(["s3", "ls", "Cowboy", "--json"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ferma/Cowboy/a.mp4" in out

    def test_meta(self, capsys):
        s3 = self._fake_s3()
        with patch("src.runner.cli._s3_client", return_value=s3):
            rc = cli.main(["s3", "meta", "Cowboy"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "trend" in out

    def test_meta_missing_returns_one(self):
        s3 = self._fake_s3()
        s3.read_meta.return_value = None
        with patch("src.runner.cli._s3_client", return_value=s3):
            rc = cli.main(["s3", "meta", "Nope"])
        assert rc == 1

    def test_pull_folder_with_limit(self, tmp_path, capsys):
        s3 = self._fake_s3()
        s3.download.side_effect = lambda key, dest: dest
        with patch("src.runner.cli._s3_client", return_value=s3):
            rc = cli.main(
                ["s3", "pull", "Cowboy", "--dest", str(tmp_path), "--limit", "1"]
            )
        assert rc == 0
        assert s3.download.call_count == 1  # limited to 1

    def test_pull_single_key(self, tmp_path):
        s3 = self._fake_s3()
        s3.download.side_effect = lambda key, dest: dest
        with patch("src.runner.cli._s3_client", return_value=s3):
            rc = cli.main(
                ["s3", "pull", "--key", "ferma/Cowboy/a.mp4", "--dest",
                 str(tmp_path / "a.mp4")]
            )
        assert rc == 0
        s3.download.assert_called_once()

    def test_pull_needs_target(self):
        with patch("src.runner.cli._s3_client", return_value=self._fake_s3()):
            rc = cli.main(["s3", "pull"])
        assert rc == 2
