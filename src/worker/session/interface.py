"""MobileWorker — backend-agnostic interface scoped to one physical device.

All callers interact with MobileWorker; the Mobilerun adapter (MVP) and any
future Appium adapter implement this interface identically.
"""

from __future__ import annotations

import abc
from typing import Any


class MobileWorker(abc.ABC):
    """Per-device session wrapper.

    Every instance is bound to exactly one device_serial at construction time.
    All actions target only that device.
    """

    @property
    @abc.abstractmethod
    def device_serial(self) -> str:
        """ADB serial (USB or ip:port) of the bound device."""

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Whether the session is currently usable."""

    # --- lifecycle ---

    @abc.abstractmethod
    def connect(self) -> None:
        """Establish or re-establish the device session."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Tear down the session, releasing resources."""

    # --- screen inspection ---

    @abc.abstractmethod
    def screenshot(self, label: str = "") -> bytes:
        """Capture a PNG screenshot. Returns raw bytes."""

    @abc.abstractmethod
    def page_source(self) -> str:
        """Return the current UI hierarchy as XML."""

    # --- interaction ---

    @abc.abstractmethod
    def tap(self, x: int, y: int) -> None:
        """Tap at screen coordinates."""

    @abc.abstractmethod
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        """Swipe between two points."""

    @abc.abstractmethod
    def type_text(self, text: str) -> None:
        """Type text into the currently focused field."""

    # --- high-level agent execution ---

    @abc.abstractmethod
    def run_goal(
        self,
        goal: str,
        *,
        output_model: type | None = None,
        variables: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Execute a natural-language goal on the device via the backend agent.

        Returns the structured result from the agent (shape depends on output_model).
        """
