"""Mobilerun adapter — implements MobileWorker via GenFarmer / MobileAgent.

Wraps farm/agent.py's build_agent shape:
    (goal, device_serial, output_model, variables, overrides, timeout)
behind the MobileWorker interface.
"""

from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import subprocess
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.worker.session.interface import MobileWorker

logger = logging.getLogger(__name__)


class MobilerunRouteMissingError(RuntimeError):
    """Raised when GenFarmer does not expose an expected automation route."""


class MobilerunWorker(MobileWorker):
    """Mobilerun-backed MobileWorker scoped to one device."""

    def __init__(
        self,
        device_serial: str,
        genfarmer_url: str = "http://127.0.0.1:55554",
        config_overrides: dict[str, Any] | None = None,
        adb_fallback: bool = True,
        use_tcp: bool | None = None,
    ) -> None:
        self._device_serial = device_serial
        self._genfarmer_url = genfarmer_url.rstrip("/")
        self._config_overrides = config_overrides or {}
        self._adb_fallback = adb_fallback
        self._use_tcp = _truthy(os.environ.get("MOBILERUN_USE_TCP", "1")) if use_tcp is None else use_tcp
        self._connected = False
        self._user_id: str | None = None
        self._actions_log: list[dict[str, Any]] = []
        self._driver: Any | None = None

    @property
    def device_serial(self) -> str:
        return self._device_serial

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- lifecycle ---

    def connect(self) -> None:
        data = self._api_get("/backend/auth/me")
        self._user_id = data.get("id") or data.get("userId")
        self._connected = True
        self._log_action("connect", {"user_id": self._user_id})

    def disconnect(self) -> None:
        self._connected = False
        self._driver = None
        self._log_action("disconnect")

    # --- screen inspection ---

    def screenshot(self, label: str = "") -> bytes:
        self._ensure_connected()
        fallback_used = False
        try:
            raw = self._mobilerun_screenshot()
        except Exception as e:
            if not self._adb_fallback:
                raise
            raw = self._adb_screenshot()
            fallback_used = True
            self._log_action("adb_fallback", {"operation": "screenshot", "error": str(e)[:200]})
        self._log_action(
            "screenshot",
            {
                "label": label,
                "size": len(raw),
                "driver": self._driver_name(),
                "fallback_used": fallback_used,
            },
        )
        return raw

    def page_source(self) -> str:
        self._ensure_connected()
        fallback_used = False
        try:
            source = self._mobilerun_page_source()
            if not self._source_has_nodes(str(source)):
                raise RuntimeError("empty page_source")
        except Exception as e:
            if not self._adb_fallback:
                raise
            source = self._adb_page_source()
            fallback_used = True
            self._log_action("adb_fallback", {"operation": "page_source", "error": str(e)[:200]})
        self._log_action(
            "page_source",
            {
                "length": len(source),
                "driver": self._driver_name(),
                "fallback_used": fallback_used,
            },
        )
        return source

    # --- interaction ---

    def tap(self, x: int, y: int) -> None:
        self._ensure_connected()
        fallback_used = False
        try:
            self._mobilerun_tap(x, y)
        except Exception as e:
            if not self._adb_fallback:
                raise
            self._adb_shell(f"input tap {int(x)} {int(y)}")
            fallback_used = True
            self._log_action("adb_fallback", {"operation": "tap", "error": str(e)[:200]})
        self._log_action(
            "tap",
            {
                "x": x,
                "y": y,
                "driver": self._driver_name(),
                "fallback_used": fallback_used,
            },
        )

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._ensure_connected()
        fallback_used = False
        try:
            self._mobilerun_swipe(x1, y1, x2, y2, duration_ms)
        except Exception as e:
            if not self._adb_fallback:
                raise
            self._adb_shell(
                f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}"
            )
            fallback_used = True
            self._log_action("adb_fallback", {"operation": "swipe", "error": str(e)[:200]})
        self._log_action(
            "swipe",
            {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "duration_ms": duration_ms,
                "driver": self._driver_name(),
                "fallback_used": fallback_used,
            },
        )

    def type_text(self, text: str) -> None:
        self._ensure_connected()
        fallback_used = False
        try:
            self._mobilerun_type_text(text)
        except Exception as e:
            if not self._adb_fallback:
                raise
            self._adb_input_text(text)
            fallback_used = True
            self._log_action("adb_fallback", {"operation": "type_text", "error": str(e)[:200]})
        self._log_action(
            "type_text",
            {
                "length": len(text),
                "driver": self._driver_name(),
                "fallback_used": fallback_used,
            },
        )

    def open_app(
        self,
        package: str,
        *,
        activity: str | None = None,
        force_stop: bool = False,
        wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Open an app through ADB.

        The deployed GenFarmer build does not expose imperative app-control
        routes such as /automation/run or /automation/open_app. Launching via
        ADB is the confirmed non-destructive path on the VPS.
        """
        self._ensure_connected()
        if force_stop:
            self._adb_shell(f"am force-stop {package}", timeout=20)
            time.sleep(0.4)

        if activity:
            component = f"{package}/{activity}"
            out = self._adb_shell(f"am start -n {component}", timeout=20)
        else:
            out = self._adb_shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
                timeout=20,
            )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        activity_out = self.current_activity()
        ok = package in activity_out
        result = {
            "status": "success" if ok else "error",
            "package": package,
            "activity": activity_out,
            "output": out.strip()[:500],
        }
        self._log_action("open_app", result)
        return result

    def current_activity(self) -> str:
        """Return the current resumed activity text, best-effort."""
        self._ensure_connected()
        try:
            out = self._adb_shell(
                "dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity'",
                timeout=10,
            )
        except Exception:
            out = self._adb_shell("dumpsys activity top | head -n 40", timeout=10)
        return out.strip()

    def activity_page_source(self) -> str:
        """Return Android activity dump for fallback resource-id parsing."""
        self._ensure_connected()
        source = self._adb_shell("dumpsys activity top", timeout=20)
        self._log_action("activity_page_source", {"length": len(source)})
        return source

    # --- high-level agent execution ---

    def run_goal(
        self,
        goal: str,
        *,
        output_model: type | None = None,
        variables: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        self._ensure_connected()
        merged_overrides = {**self._config_overrides, **(overrides or {})}
        payload: dict[str, Any] = {
            "goal": goal,
            "deviceSerial": self._device_serial,
            "variables": variables or {},
            "overrides": merged_overrides,
            "timeoutSeconds": timeout_seconds,
        }
        if output_model is not None:
            payload["outputModel"] = output_model.__name__

        self._log_action("run_goal", {"goal_preview": goal[:80], "timeout": timeout_seconds})
        result = self._api_post("/automation/run", payload, timeout=timeout_seconds + 30)
        self._log_action("run_goal_complete", {"status": result.get("status")})
        return result

    # --- action log (for observability / artifact capture) ---

    @property
    def actions_log(self) -> list[dict[str, Any]]:
        return list(self._actions_log)

    def preflight_ui_tree(self) -> dict[str, Any]:
        """Check current foreground activity and UI-tree availability.

        This is a non-posting readiness probe. It prefers the same page_source
        path normal callers use, so a working result may come from Mobilerun or
        from the ADB/uiautomator fallback.
        """
        self._ensure_connected()
        activity = ""
        try:
            activity = self._adb_shell(
                "dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity'",
                timeout=10,
            ).strip()
        except Exception:
            pass
        source = self.page_source()
        node_count = self._source_node_count(source)
        result = {
            "device_serial": self._device_serial,
            "activity": activity,
            "ui_tree_available": node_count > 0,
            "ui_tree_count": node_count,
            "source": "mobilerun_tcp" if self._use_tcp else "mobilerun",
            "use_tcp": self._use_tcp,
            "adb_fallback": any(
                action.get("action") == "adb_fallback"
                and action.get("details", {}).get("operation") == "page_source"
                for action in self._actions_log
            ),
        }
        self._log_action("preflight_ui_tree", result)
        return result

    # --- internals ---

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError(f"MobilerunWorker not connected (device={self._device_serial})")

    def _log_action(self, action: str, details: dict[str, Any] | None = None) -> None:
        entry = {
            "action": action,
            "device_serial": self._device_serial,
            "timestamp": time.time(),
        }
        if details:
            entry["details"] = details
        self._actions_log.append(entry)
        logger.debug("MobilerunWorker action: %s %s", action, details or "")

    def _driver_name(self) -> str:
        return "mobilerun_tcp" if self._use_tcp else "mobilerun"

    def _api_get(self, path: str, timeout: int = 10) -> dict[str, Any]:
        url = f"{self._genfarmer_url}{path}"
        req = Request(url, headers={"Accept": "application/json"})
        try:
            resp = urlopen(req, timeout=timeout)
        except HTTPError as e:
            if e.code == 404:
                raise MobilerunRouteMissingError(
                    f"GenFarmer route missing: GET {path} returned 404"
                ) from e
            raise
        return json.loads(resp.read())

    def _api_post(self, path: str, body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        url = f"{self._genfarmer_url}{path}"
        data = json.dumps(body).encode()
        req = Request(url, data=data, headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            resp = urlopen(req, timeout=timeout)
        except HTTPError as e:
            if e.code == 404:
                raise MobilerunRouteMissingError(
                    f"GenFarmer route missing: POST {path} returned 404"
                ) from e
            raise
        return json.loads(resp.read())

    def _await_if_needed(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            import asyncio

            return asyncio.run(value)
        return value

    def _mobilerun_driver(self) -> Any:
        if self._driver is not None:
            return self._driver
        try:
            from mobilerun import AndroidDriver
        except ModuleNotFoundError as exc:
            raise RuntimeError("mobilerun is not installed") from exc
        driver = AndroidDriver(serial=self._device_serial, use_tcp=self._use_tcp)
        self._await_if_needed(driver.connect())
        self._driver = driver
        self._log_action("mobilerun_connect", {"use_tcp": self._use_tcp})
        return driver

    def _mobilerun_page_source(self) -> str:
        payload = self._await_if_needed(self._mobilerun_driver().get_ui_tree())
        source = json.dumps(payload, ensure_ascii=False)
        self._log_action("mobilerun_page_source", {"use_tcp": self._use_tcp, "length": len(source)})
        return source

    def _mobilerun_screenshot(self) -> bytes:
        raw = self._await_if_needed(self._mobilerun_driver().screenshot())
        if isinstance(raw, str):
            return base64.b64decode(raw)
        return bytes(raw or b"")

    def _mobilerun_tap(self, x: int, y: int) -> None:
        self._await_if_needed(self._mobilerun_driver().tap(int(x), int(y)))

    def _mobilerun_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._await_if_needed(
            self._mobilerun_driver().swipe(
                int(x1), int(y1), int(x2), int(y2), duration_ms=float(duration_ms)
            )
        )

    def _mobilerun_type_text(self, text: str) -> None:
        ok = self._await_if_needed(self._mobilerun_driver().input_text(text))
        if ok is False:
            raise RuntimeError("mobilerun input_text returned false")

    def _adb_path(self) -> str:
        return os.environ.get("ADB_PATH", "adb")

    def _adb(self, args: list[str], *, timeout: int = 60, text: bool = True) -> subprocess.CompletedProcess:
        cmd = [self._adb_path(), "-s", self._device_serial, *args]
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            timeout=timeout,
        )
        if proc.returncode != 0:
            out = proc.stderr if text else proc.stderr.decode(errors="replace")
            if not out:
                out = proc.stdout if text else proc.stdout.decode(errors="replace")
            raise RuntimeError(f"adb rc={proc.returncode}: {str(out).strip()[:300]}")
        return proc

    def _adb_shell(self, command: str, *, timeout: int = 60) -> str:
        proc = self._adb(["shell", command], timeout=timeout, text=True)
        return str(proc.stdout or "")

    def _adb_screenshot(self) -> bytes:
        proc = self._adb(["exec-out", "screencap", "-p"], timeout=30, text=False)
        return bytes(proc.stdout or b"")

    def _adb_page_source(self) -> str:
        remote = "/sdcard/window.xml"
        try:
            self._adb_shell(f"uiautomator dump {remote}", timeout=30)
        except RuntimeError:
            source = self._adb_shell(f"cat {remote}", timeout=30)
            if self._source_has_nodes(source):
                return source
            raise
        return self._adb_shell(f"cat {remote}", timeout=30)

    def _adb_input_text(self, text: str) -> None:
        escaped = (
            text.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace(" ", "%s")
            .replace('"', '\\"')
            .replace("'", "\\'")
        )
        self._adb_shell(f"input text \"{escaped}\"", timeout=30)

    def _source_has_nodes(self, source: str) -> bool:
        stripped = source.strip()
        if not stripped:
            return False
        if "<node" in stripped:
            return True
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return True
        return self._json_has_nodes(data)

    def _json_has_nodes(self, data: Any) -> bool:
        return self._json_node_count(data) > 0

    def _source_node_count(self, source: str) -> int:
        stripped = source.strip()
        if not stripped:
            return 0
        if "<node" in stripped:
            return stripped.count("<node")
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return 0
        return self._json_node_count(data)

    def _json_node_count(self, data: Any) -> int:
        if isinstance(data, list):
            return sum(self._json_node_count(item) for item in data)
        if isinstance(data, dict):
            node_keys = {
                "text",
                "resourceId",
                "resource-id",
                "contentDescription",
                "content-desc",
                "bounds",
                "className",
                "class",
            }
            count = 1 if any(key in data for key in node_keys) else 0
            return count + sum(self._json_node_count(value) for value in data.values())
        return 0


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
