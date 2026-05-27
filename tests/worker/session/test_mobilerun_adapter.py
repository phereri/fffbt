"""Tests for MobilerunWorker — unit tests using mocked HTTP responses."""

from __future__ import annotations

import json
import sys
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import pytest

from src.worker.session.mobilerun_adapter import MobilerunRouteMissingError, MobilerunWorker


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
        status = 200
        if isinstance(body, tuple):
            status, body = body
        payload = json.dumps(body).encode()
        self.send_response(status)
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

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker._mobilerun_screenshot = lambda: png_data
    result = worker.screenshot(label="test_capture")
    assert result == png_data


def test_page_source_uses_mobilerun_tcp(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker._mobilerun_page_source = lambda: json.dumps({
        "a11y_tree": {
            "className": "android.widget.FrameLayout",
            "children": [{"text": "Home", "className": "android.widget.TextView"}],
        }
    })
    source = worker.page_source()
    assert "Home" in source
    assert worker._use_tcp is True


def test_tap(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    calls = []
    worker._mobilerun_tap = lambda x, y: calls.append((x, y))
    worker.tap(100, 200)
    assert calls == [(100, 200)]
    assert any(a["action"] == "tap" for a in worker.actions_log)


def test_swipe(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    calls = []
    worker._mobilerun_swipe = lambda *args: calls.append(args)
    worker.swipe(100, 1500, 100, 500, duration_ms=400)
    assert calls == [(100, 1500, 100, 500, 400)]
    log = next(a for a in worker.actions_log if a["action"] == "swipe")
    assert log["details"]["x1"] == 100


def test_type_text(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    calls = []
    worker._mobilerun_type_text = lambda text: calls.append(text)
    worker.type_text("hello world")
    assert calls == ["hello world"]
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


def test_run_goal_route_missing_has_clear_error(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}
    handler.responses[("POST", "/automation/run")] = (404, {"error": "Endpoint not found"})

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    with pytest.raises(MobilerunRouteMissingError, match="POST /automation/run returned 404"):
        worker.run_goal("Open Instagram", timeout_seconds=1)


def test_open_app_uses_adb(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True
    calls: list[str] = []

    def shell(command, **kwargs):
        calls.append(command)
        if "dumpsys activity" in command:
            return "topResumedActivity=ActivityRecord{ com.instagram.android/.MainActivity }"
        return "OK"

    monkeypatch.setattr(worker, "_adb_shell", shell)

    result = worker.open_app(
        "com.instagram.android",
        activity="com.instagram.mainactivity.InstagramMainActivity",
        force_stop=True,
        wait_seconds=0,
    )

    assert result["status"] == "success"
    assert calls[0] == "am force-stop com.instagram.android"
    assert calls[1].startswith("am start -n com.instagram.android/")


def test_actions_not_connected():
    worker = MobilerunWorker("DEV", genfarmer_url="http://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="not connected"):
        worker.tap(0, 0)


def test_actions_log_records_all(mock_server):
    url, handler = mock_server
    handler.responses[("GET", "/backend/auth/me")] = {"id": "u1"}

    worker = MobilerunWorker("DEV", genfarmer_url=url)
    worker.connect()
    worker._mobilerun_tap = lambda x, y: None
    worker._mobilerun_page_source = lambda: '<hierarchy><node text="x" /></hierarchy>'
    worker.tap(10, 20)
    worker.page_source()
    actions = [a["action"] for a in worker.actions_log]
    assert actions == ["connect", "tap", "page_source"]


def test_page_source_falls_back_to_adb_on_empty_mobilerun(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True
    monkeypatch.setattr(worker, "_mobilerun_page_source", lambda: "")
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

    monkeypatch.setattr(worker, "_mobilerun_screenshot", fail)
    monkeypatch.setattr(worker, "_adb_screenshot", lambda: b"\x89PNG\r\n\x1a\nadb")

    assert worker.screenshot("fallback") == b"\x89PNG\r\n\x1a\nadb"
    assert any(a["details"]["operation"] == "screenshot" for a in worker.actions_log if a["action"] == "adb_fallback")


def test_tap_falls_back_to_adb_on_api_error(monkeypatch):
    worker = MobilerunWorker("DEV")
    worker._connected = True
    shell_calls = []

    def fail(*args, **kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(worker, "_mobilerun_tap", fail)
    monkeypatch.setattr(worker, "_adb_shell", lambda command, **kwargs: shell_calls.append(command) or "")

    worker.tap(10, 20)

    assert shell_calls == ["input tap 10 20"]
    assert any(a["action"] == "tap" for a in worker.actions_log)


def test_mobilerun_driver_uses_tcp_by_default(monkeypatch):
    created = {}

    class FakeDriver:
        def __init__(self, serial, use_tcp):
            created["serial"] = serial
            created["use_tcp"] = use_tcp

        def connect(self):
            return None

    fake_module = types.ModuleType("mobilerun")
    fake_module.AndroidDriver = FakeDriver
    monkeypatch.setitem(sys.modules, "mobilerun", fake_module)
    worker = MobilerunWorker("DEV")
    worker._connected = True

    assert worker._mobilerun_driver() is not None
    assert created == {"serial": "DEV", "use_tcp": True}


def test_mobilerun_driver_honors_tcp_env_disabled(monkeypatch):
    created = {}

    class FakeDriver:
        def __init__(self, serial, use_tcp):
            created["use_tcp"] = use_tcp

        def connect(self):
            return None

    monkeypatch.setenv("MOBILERUN_USE_TCP", "0")
    fake_module = types.ModuleType("mobilerun")
    fake_module.AndroidDriver = FakeDriver
    monkeypatch.setitem(sys.modules, "mobilerun", fake_module)
    worker = MobilerunWorker("DEV")
    worker._connected = True

    worker._mobilerun_driver()
    assert created["use_tcp"] is False


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
