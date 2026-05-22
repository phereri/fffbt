"""Unit tests for poc_appium — tests the non-network helpers only."""

from __future__ import annotations

import io
import sys

from src.worker.poc_appium import _log


def test_log_pass(capsys):
    result = _log("some_check", True, "details here")
    assert result == {"check": "some_check", "passed": True, "detail": "details here"}
    captured = capsys.readouterr()
    assert "[PASS] some_check" in captured.err
    assert "details here" in captured.err


def test_log_fail(capsys):
    result = _log("bad_check", False, "went wrong")
    assert result["passed"] is False
    captured = capsys.readouterr()
    assert "[FAIL] bad_check" in captured.err


def test_log_no_detail(capsys):
    result = _log("simple", True)
    assert result["detail"] == ""
    captured = capsys.readouterr()
    assert "simple" in captured.err
    assert "—" not in captured.err


def test_run_poc_server_unreachable(tmp_path):
    from src.worker.poc_appium import run_poc

    results = run_poc("http://127.0.0.1:1", "fake-serial", str(tmp_path))
    assert len(results) == 1
    assert results[0]["check"] == "appium_reachable"
    assert results[0]["passed"] is False
