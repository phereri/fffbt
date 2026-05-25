"""Unit tests for poc_mobilerun — tests the non-network helpers only."""

from __future__ import annotations

from src.worker.poc_mobilerun import _log


def test_log_pass(capsys):
    result = _log("check_a", True, "ok")
    assert result == {"check": "check_a", "passed": True, "detail": "ok"}
    captured = capsys.readouterr()
    assert "[PASS]" in captured.err


def test_log_fail(capsys):
    result = _log("check_b", False, "err")
    assert result["passed"] is False
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.err


def test_run_poc_server_unreachable(tmp_path):
    from src.worker.poc_mobilerun import run_poc

    results = run_poc("http://127.0.0.1:1", str(tmp_path))
    assert len(results) == 1
    assert results[0]["check"] == "genfarmer_reachable"
    assert results[0]["passed"] is False
