"""GenFarmer integration: device-identity rotation and app backup/restore.

A shared package (used by both autoreg and posting) wrapping GenFarmer's
on-device mechanisms:

* ``changedevice`` — rotate / capture / restore device fingerprint identity.
* ``app_backup`` — backup / restore app data via genfarmer root shell.

See ``docs/runbooks/changedevice.md`` for identity rotation details.
"""

from __future__ import annotations

from src.genfarmer.app_backup import (
    AppBackupClient,
    BackupManifest,
    BackupResult,
    RestoreResult,
    default_backup_client,
)
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
    "AppBackupClient",
    "BackupManifest",
    "BackupResult",
    "ChangeDeviceClient",
    "ChangeDeviceError",
    "DeviceProfile",
    "RestoreResult",
    "default_backup_client",
    "default_client",
]
