"""Tests for device inspection tools."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from src.worker.tools.device import (
    device_summary,
    mock_location_status,
    set_mock_location_app,
)


class TestDeviceSummary:
    @patch("src.worker.tools.device.shell", new_callable=AsyncMock)
    def test_success(self, mock_shell):
        mock_shell.return_value = "samsung\nGalaxy S21\n12\nsamsung/...\nabc123\n"
        result = asyncio.run(device_summary("DEV001"))
        assert result.success
        data = json.loads(result.message)
        assert data["brand"] == "samsung"
        assert data["model"] == "Galaxy S21"
        assert data["serial"] == "DEV001"

    @patch(
        "src.worker.tools.device.shell",
        new_callable=AsyncMock,
        side_effect=RuntimeError("offline"),
    )
    def test_error(self, mock_shell):
        result = asyncio.run(device_summary("DEV001"))
        assert not result.success
        assert "shell error" in result.message


class TestMockLocationStatus:
    @patch("src.worker.tools.device.shell", new_callable=AsyncMock)
    def test_success(self, mock_shell):
        mock_shell.side_effect = ["io.appium.settings\n", "1\n"]
        result = asyncio.run(mock_location_status("DEV001"))
        assert result.success
        data = json.loads(result.message)
        assert data["mock_location_app"] == "io.appium.settings"
        assert data["developer_options_enabled"] == "1"


class TestSetMockLocationApp:
    def test_invalid_package(self):
        result = asyncio.run(set_mock_location_app("DEV001", "invalid"))
        assert not result.success
        assert "invalid package" in result.message

    def test_empty_package(self):
        result = asyncio.run(set_mock_location_app("DEV001", ""))
        assert not result.success

    @patch("src.worker.tools.device.shell", new_callable=AsyncMock)
    def test_success(self, mock_shell):
        mock_shell.side_effect = ["", "", "io.appium.settings\n"]
        result = asyncio.run(set_mock_location_app("DEV001", "io.appium.settings"))
        assert result.success
        assert "io.appium.settings" in result.message

    @patch("src.worker.tools.device.shell", new_callable=AsyncMock)
    def test_verification_mismatch(self, mock_shell):
        mock_shell.side_effect = ["", "", "some.other.app\n"]
        result = asyncio.run(
            set_mock_location_app("DEV001", "io.appium.settings")
        )
        assert not result.success
        assert "expected" in result.message
