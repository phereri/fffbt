"""Device inspection and mock-location tools."""

from __future__ import annotations

import json
import shlex

from src.worker.tools._adb import shell
from src.worker.tools._types import ToolResult


async def device_summary(serial: str) -> ToolResult:
    """Return brand/model/android version/fingerprint/android_id as JSON."""
    try:
        getprop = await shell(
            serial,
            "getprop ro.product.brand && getprop ro.product.model && "
            "getprop ro.build.version.release && getprop ro.build.fingerprint && "
            "settings get secure android_id",
        )
        lines = [ln.strip() for ln in (getprop or "").splitlines() if ln.strip()]
        keys = ["brand", "model", "android_release", "fingerprint", "android_id"]
        info = dict(zip(keys, lines, strict=False))
        info["serial"] = serial
        return ToolResult.ok(json.dumps(info, ensure_ascii=False))
    except Exception as e:
        return ToolResult.fail(f"shell error: {e}")


async def mock_location_status(serial: str) -> ToolResult:
    """Read mock-location settings from the device."""
    try:
        app = (await shell(serial, "settings get secure mock_location_app")).strip()
        debuggable = (
            await shell(serial, "settings get global development_settings_enabled")
        ).strip()
        return ToolResult.ok(
            json.dumps(
                {"mock_location_app": app, "developer_options_enabled": debuggable}
            )
        )
    except Exception as e:
        return ToolResult.fail(f"shell error: {e}")


async def set_mock_location_app(serial: str, package: str) -> ToolResult:
    """Pin a package as the device's mock-location provider.

    Grants the appop, sets the secure setting, and verifies the result.
    """
    if not package or "." not in package:
        return ToolResult.fail(f"invalid package: {package!r}")
    try:
        await shell(
            serial,
            f"appops set {shlex.quote(package)} android:mock_location allow",
        )
        await shell(
            serial,
            f"settings put secure mock_location_app {shlex.quote(package)}",
        )
        check = (
            await shell(serial, "settings get secure mock_location_app")
        ).strip()
        if check != package:
            return ToolResult.fail(
                f"mock_location_app is {check!r}, expected {package!r}"
            )
        return ToolResult.ok(f"mock_location_app = {check}")
    except Exception as e:
        return ToolResult.fail(f"shell error: {e}")
