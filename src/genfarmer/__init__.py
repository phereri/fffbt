"""GenFarmer integration: device-identity rotation (ChangeDevice) and helpers.

A shared package (used by both autoreg and posting) wrapping GenFarmer's
on-device ``ChangeDevice`` mechanism. See ``changedevice`` for the client and
``docs/runbooks/changedevice.md`` for the full runbook.
"""

from __future__ import annotations

from src.genfarmer.changedevice import (
    API_BASE,
    PROPS_REMOTE_PATH,
    ChangeDeviceClient,
    ChangeDeviceError,
    DeviceProfile,
    default_client,
)

__all__ = [
    "API_BASE",
    "PROPS_REMOTE_PATH",
    "ChangeDeviceClient",
    "ChangeDeviceError",
    "DeviceProfile",
    "default_client",
]
