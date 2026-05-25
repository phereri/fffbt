"""Mobilerun adapter — implements MobileWorker via GenFarmer / MobileAgent.

Wraps farm/agent.py's build_agent shape:
    (goal, device_serial, output_model, variables, overrides, timeout)
behind the MobileWorker interface.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from src.worker.session.interface import MobileWorker

logger = logging.getLogger(__name__)


class MobilerunWorker(MobileWorker):
    """Mobilerun-backed MobileWorker scoped to one device."""

    def __init__(
        self,
        device_serial: str,
        genfarmer_url: str = "http://127.0.0.1:55554",
        config_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._device_serial = device_serial
        self._genfarmer_url = genfarmer_url.rstrip("/")
        self._config_overrides = config_overrides or {}
        self._connected = False
        self._user_id: str | None = None
        self._actions_log: list[dict[str, Any]] = []

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
        self._log_action("disconnect")

    # --- screen inspection ---

    def screenshot(self, label: str = "") -> bytes:
        self._ensure_connected()
        resp = self._api_post(
            "/automation/screenshot",
            {"deviceSerial": self._device_serial},
        )
        raw = resp.get("data", b"")
        if isinstance(raw, str):
            import base64
            raw = base64.b64decode(raw)
        self._log_action("screenshot", {"label": label, "size": len(raw)})
        return raw

    def page_source(self) -> str:
        self._ensure_connected()
        resp = self._api_post(
            "/automation/page_source",
            {"deviceSerial": self._device_serial},
        )
        source = resp.get("data", "")
        self._log_action("page_source", {"length": len(source)})
        return source

    # --- interaction ---

    def tap(self, x: int, y: int) -> None:
        self._ensure_connected()
        self._api_post(
            "/automation/tap",
            {"deviceSerial": self._device_serial, "x": x, "y": y},
        )
        self._log_action("tap", {"x": x, "y": y})

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._ensure_connected()
        self._api_post(
            "/automation/swipe",
            {
                "deviceSerial": self._device_serial,
                "x1": x1, "y1": y1,
                "x2": x2, "y2": y2,
                "durationMs": duration_ms,
            },
        )
        self._log_action("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms})

    def type_text(self, text: str) -> None:
        self._ensure_connected()
        self._api_post(
            "/automation/type",
            {"deviceSerial": self._device_serial, "text": text},
        )
        self._log_action("type_text", {"length": len(text)})

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

    def _api_get(self, path: str, timeout: int = 10) -> dict[str, Any]:
        url = f"{self._genfarmer_url}{path}"
        req = Request(url, headers={"Accept": "application/json"})
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read())

    def _api_post(self, path: str, body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        url = f"{self._genfarmer_url}{path}"
        data = json.dumps(body).encode()
        req = Request(url, data=data, headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read())
