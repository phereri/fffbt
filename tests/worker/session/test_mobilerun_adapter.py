"""Tests for MobilerunWorker — unit tests using mocked HTTP responses."""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import pytest

from src.worker.session.mobilerun_adapter import MobilerunWorker


class _MockHandler(BaseHTTPRequestHandler):
    responses: dict[str, Any] = {}

    def do_GET(self):
        body = self.responses.get(("GET", self.path), {"error": "not found"})
        self._respond(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = self.responses.get(("POST", self.path), {"error": "not found"})
        self._respond(body)

    def _respond(self, body: Any):
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


@pytest.fixture()
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", _MockHandler
    server.shutdown()


def test_connect_success(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "user-123"}

    worker = MobilerunWorker("DEVICE001", genfarmer_url=url)
    assert not worker.is_connected
    worker.connect()
    assert worker.is_connected
    assert worker.device_serial == "DEVICE001"


def test_connect_failure_raises(mock_server):
    worker = MobilerunWorker("DEV", genfarmer_url="http://127.0.0.1:1")
    with pytest.raises(Exception):
        worker.connect()


def test_disconnect(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    assert worker.is_connected
    worker.disconnect()
    assert not worker.is_connected


def test_screenshot(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    png_data = b"\x89PNG\r\n\x1a\nfakedata"
    handler.responses[("POST", "/automation/screenshot")] = {
        "data": base64.b64encode(png_data).decode()
    }

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    result = worker.screenshot(label="test_capture")
    assert result == png_data


def test_page_source(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/page_source")] = {
        "data": "<hierarchy><node /></hierarchy>"
    }

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    source = worker.page_source()
    assert "<hierarchy>" in source


def test_tap(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/tap")] = {"ok": True}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker.tap(100, 200)
    assert any(a["action"] == "tap" for a in worker.actions_log)


def test_swipe(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/swipe")] = {"ok": True}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker.swipe(100, 1500, 100, 500, duration_ms=400)
    log = next(a for a in worker.actions_log if a["action"] == "swipe")
    assert log["details"]["x1"] == 100


def test_type_text(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/type")] = {"ok": True}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker.type_text("hello world")
    log = next(a for a in worker.actions_log if a["action"] == "type_text")
    assert log["details"]["length"] == 11


def test_run_goal(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/run")] = {"status": "success", "output": {"posted": True}}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    result = worker.run_goal("Post a Trial Reel", timeout_seconds=60)
    assert result["status"] == "success"


def test_actions_not_connected():
    worker = MobilerunWorker("DEV", genfarmer_url="http://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="not connected"):
        worker.tap(0, 0)


def test_actions_log_records_all(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/tap")] = {"ok": True}
    handler.responses[("POST", "/automation/page_source")] = {"data": "<x/>"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker.tap(10, 20)
    worker.page_source()
    actions = [a["action"] for a in worker.actions_log]
    assert actions == ["connect", "tap", "page_source"]


def test_page_source_falls_back_to_adb_on_empty_api(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True
    monkeypatch.setattr(worker, "_api_post", lambda *args, **kwargs: {"data": ""})
    monkeypatch.setattr(
        worker,
        "_adb_page_source",
        lambda: '<hierarchy><node text="Instagram" /></hierarchy>',
    )

    source = worker.page_source()

    assert "Instagram" in source
    actions = [a["action"] for a in worker.actions_log]
    assert "adb_fallback" in actions
    assert actions[-1] == "page_source"


def test_screenshot_falls_back_to_adb_on_api_error(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True

    def fail(*args, **kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(worker, "_api_post", fail)
    monkeypatch.setattr(worker, "_adb_screenshot", lambda: b"\x89PNG\r\n\x1a\nadb")

    assert worker.screenshot("fallback") == b"\x89PNG\r\n\x1a\nadb"
    assert any(a["details"]["operation"] == "screenshot" for a in worker.actions_log if a["action"] == "adb_fallback")


def test_tap_falls_back_to_adb_on_api_error(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True
    shell_calls = []

    def fail(*args, **kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(worker, "_api_post", fail)
    monkeypatch.setattr(worker, "_adb_shell", lambda command, **kwargs: shell_calls.append(command) or "")

    worker.tap(10, 20)

    assert shell_calls == ["input tap 10 20"]
    assert any(a["action"] == "tap" for a in worker.actions_log)


def test_preflight_ui_tree_reports_node_count(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True
    monkeypatch.setattr(
        worker,
        "_adb_shell",
        lambda command, **kwargs: "topResumedActivity=ActivityRecord{ com.instagram.android/.MainActivity }",
    )
    monkeypatch.setattr(
        worker,
        "page_source",
        lambda: '<hierarchy><node text="A" /><node text="B" /></hierarchy>',
    )

    result = worker.preflight_ui_tree()

    assert result["ui_tree_available"] is True
    assert result["ui_tree_count"] == 2
    assert "com.instagram.android" in result["activity"]
