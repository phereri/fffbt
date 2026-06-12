"""GenFarmer **AppBackup** — backup/restore app data via genfarmer root shell.

Mechanism (from GenBR ``genfarmer_app_backup.py``, validated on SM-G781B):
``genfarmer -c`` gives a root shell on the device. We tar the app's
``/data/data/<pkg>/`` subdirs, pull the archive via adb, and save a manifest.

Restore is the reverse: push archive, extract via genfarmer, chown + restorecon.

This does NOT use ``adb backup`` (which needs on-screen confirmation and doesn't
work reliably on GenFarmer ROM). It directly accesses ``/data/data/`` via root.

Key steps:
  backup:  force-stop → genfarmer tar → adb pull → manifest
  restore: force-stop → adb push → genfarmer untar → chown → restorecon

I/O is injectable for testability. :func:`default_backup_client` wires real
adb + genfarmer shell for use on the GenFarmer Windows host.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

GENFARMER_BIN = "/system/bin/genfarmer"
DEVICE_BACKUP_DIR = "/storage/emulated/0/backup"
DEVICE_BACKUP_TAR = "backup.tar.gz"
MANIFEST_NAME = "manifest.json"

# Directories under /data/data/<pkg>/ that matter for session restore.
# Instagram: shared_prefs has session tokens, databases has local state.
DEFAULT_BACKUP_PATHS = [
    "shared_prefs",
    "databases",
    "no_backup",
    "files",
    "code_cache",
]


# ---------------------------------------------------------------------------
# Types (injectable I/O)
# ---------------------------------------------------------------------------

# (serial, command, timeout) -> stdout
AdbShellFn = Callable[[str, str, float], str]
# (serial, inner_command, timeout) -> stdout
GenfarmerShellFn = Callable[[str, str, float], str]
# (serial, remote_path, local_path, timeout) -> None  (raises on failure)
AdbPullFn = Callable[[str, str, str, float], None]
# (serial, local_path, remote_path, timeout) -> None  (raises on failure)
AdbPushFn = Callable[[str, str, str, float], None]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BackupManifest:
    """Metadata for a saved app backup."""

    serial: str
    package: str
    uid: int
    rel_paths: list[str]
    archive_name: str
    created_at: str = ""
    app_data_base: str = ""

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "package": self.package,
            "uid": self.uid,
            "rel_paths": self.rel_paths,
            "archive_name": self.archive_name,
            "created_at": self.created_at,
            "app_data_base": self.app_data_base,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BackupManifest":
        return cls(
            serial=d["serial"],
            package=d["package"],
            uid=d.get("uid", 0),
            rel_paths=d.get("rel_paths", []),
            archive_name=d.get("archive_name", d.get("archive", "data.tgz")),
            created_at=d.get("created_at", ""),
            app_data_base=d.get("app_data_base", ""),
        )


@dataclass
class BackupResult:
    """Result of a backup operation."""

    ok: bool
    backup_dir: Path | None = None
    manifest: BackupManifest | None = None
    error: str = ""
    archive_size_bytes: int = 0


@dataclass
class RestoreResult:
    """Result of a restore operation."""

    ok: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AppBackupClient:
    """Backup/restore app data directories on a GenFarmer device.

    All filesystem I/O is injected so the class is testable without a device.
    """

    def __init__(
        self,
        *,
        shell: AdbShellFn,
        genfarmer_shell: GenfarmerShellFn,
        pull: AdbPullFn,
        push: AdbPushFn,
        backup_root: Path | None = None,
        backup_paths: list[str] | None = None,
        genfarmer_bin: str = GENFARMER_BIN,
        device_backup_dir: str = DEVICE_BACKUP_DIR,
        timeout: float = 120.0,
    ):
        self._shell = shell
        self._gf_shell = genfarmer_shell
        self._pull = pull
        self._push = push
        self._backup_root = backup_root or Path("app_backups")
        self._backup_paths = backup_paths or list(DEFAULT_BACKUP_PATHS)
        self._gf_bin = genfarmer_bin
        self._device_backup_dir = device_backup_dir
        self._timeout = timeout

    def _data_base(self, package: str) -> str:
        return f"/data/data/{package}"

    def _remote_tar(self) -> str:
        return f"{self._device_backup_dir}/{DEVICE_BACKUP_TAR}"

    def _get_uid(self, serial: str, package: str) -> int:
        """Get the UID of an installed package."""
        out = self._shell(serial, f"pm list packages -U {package}", 20.0)
        m = re.search(r"uid:(\d+)", out)
        if not m:
            raise RuntimeError(f"Cannot get uid for {package}: {out}")
        return int(m.group(1))

    def _package_installed(self, serial: str, package: str) -> bool:
        try:
            out = self._shell(serial, f"pm path {package}", 20.0)
            return "package:" in out
        except RuntimeError:
            return False

    def _existing_paths(self, serial: str, package: str) -> list[str]:
        """Check which backup subdirs actually exist on device."""
        base = self._data_base(package)
        existing = []
        for rel in self._backup_paths:
            try:
                out = self._gf_shell(
                    serial,
                    f"test -e {base}/{rel} && echo YES || echo NO",
                    15.0,
                )
                if "YES" in out:
                    existing.append(rel)
            except RuntimeError:
                pass
        return existing

    def backup(
        self,
        serial: str,
        package: str,
        *,
        label: str | None = None,
        out_dir: Path | None = None,
        force_stop: bool = True,
    ) -> BackupResult:
        """Create a backup of the app's data directories.

        Returns BackupResult with the local directory containing data.tgz + manifest.json.
        """
        if not self._package_installed(serial, package):
            return BackupResult(ok=False, error=f"Package not installed: {package}")

        existing = self._existing_paths(serial, package)
        if not existing:
            return BackupResult(ok=False, error=f"No data dirs found for {package}")

        try:
            uid = self._get_uid(serial, package)
        except RuntimeError as e:
            return BackupResult(ok=False, error=str(e))

        # Determine output directory
        if out_dir:
            dest = out_dir
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            name = re.sub(r"[^a-zA-Z0-9._-]+", "_", label.strip()) if label else stamp
            dest = self._backup_root / _safe_serial(serial) / package / name

        dest.mkdir(parents=True, exist_ok=True)
        archive_local = dest / "data.tgz"
        remote_tar = self._remote_tar()

        # Force-stop the app to get a consistent snapshot
        if force_stop:
            try:
                self._shell(serial, f"am force-stop {package}", 10.0)
                time.sleep(0.5)
            except RuntimeError:
                pass  # non-critical

        # Create tar on device via genfarmer root shell
        base = self._data_base(package)
        abs_paths = [f"{base}/{rel}" for rel in existing]
        tar_cmd = (
            f"mkdir -p {self._device_backup_dir} && "
            f"tar -czf {shlex.quote(remote_tar)} "
            + " ".join(shlex.quote(p) for p in abs_paths)
        )

        try:
            self._gf_shell(serial, tar_cmd, self._timeout)
        except RuntimeError as e:
            return BackupResult(ok=False, error=f"tar failed: {e}")

        # Pull archive to local
        try:
            self._pull(serial, remote_tar, str(archive_local), self._timeout)
        except RuntimeError as e:
            return BackupResult(ok=False, error=f"adb pull failed: {e}")

        # Clean up remote tar
        try:
            self._gf_shell(serial, f"rm -f {remote_tar}", 10.0)
        except RuntimeError:
            pass

        # Write manifest
        manifest = BackupManifest(
            serial=serial,
            package=package,
            uid=uid,
            rel_paths=existing,
            archive_name=archive_local.name,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            app_data_base=base,
        )
        manifest_path = dest / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        size = archive_local.stat().st_size
        return BackupResult(
            ok=True,
            backup_dir=dest,
            manifest=manifest,
            archive_size_bytes=size,
        )

    def restore(
        self,
        serial: str,
        package: str,
        backup_dir: Path,
        *,
        force_stop: bool = True,
        restorecon: bool = True,
    ) -> RestoreResult:
        """Restore a previously saved backup to the device.

        The package must already be installed. Restores files and fixes ownership.
        """
        manifest_path = backup_dir / MANIFEST_NAME
        if not manifest_path.is_file():
            return RestoreResult(ok=False, error=f"No {MANIFEST_NAME} in {backup_dir}")

        manifest = BackupManifest.from_dict(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        archive_local = backup_dir / manifest.archive_name
        if not archive_local.is_file():
            return RestoreResult(ok=False, error=f"Archive not found: {archive_local}")

        if not self._package_installed(serial, package):
            return RestoreResult(ok=False, error=f"Package not installed: {package}")

        try:
            uid = self._get_uid(serial, package)
        except RuntimeError as e:
            return RestoreResult(ok=False, error=str(e))

        base = self._data_base(package)
        remote_tar = self._remote_tar()

        # Force-stop before restore
        if force_stop:
            try:
                self._shell(serial, f"am force-stop {package}", 10.0)
                time.sleep(0.5)
            except RuntimeError:
                pass

        # Push archive to device
        try:
            self._push(serial, str(archive_local), remote_tar, self._timeout)
        except RuntimeError as e:
            return RestoreResult(ok=False, error=f"adb push failed: {e}")

        # Extract via genfarmer root shell
        try:
            self._gf_shell(serial, f"tar -xzf {shlex.quote(remote_tar)} -C /", self._timeout)
        except RuntimeError as e:
            return RestoreResult(ok=False, error=f"tar extract failed: {e}")

        # Fix ownership
        try:
            self._gf_shell(
                serial,
                f"find {shlex.quote(base)} -exec chown {uid}:{uid} {{}} +",
                self._timeout,
            )
        except RuntimeError as e:
            return RestoreResult(ok=False, error=f"chown failed: {e}")

        # Restore SELinux contexts
        if restorecon:
            try:
                self._gf_shell(serial, f"restorecon -R {shlex.quote(base)}", self._timeout)
            except RuntimeError:
                pass  # non-critical on some ROMs

        # Clean up remote tar
        try:
            self._gf_shell(serial, f"rm -f {remote_tar}", 10.0)
        except RuntimeError:
            pass

        return RestoreResult(ok=True)


# ---------------------------------------------------------------------------
# Default wiring (real adb + genfarmer on GenFarmer host)
# ---------------------------------------------------------------------------


def _default_adb_bin() -> str:
    return os.environ.get("ADB") or os.environ.get("ADB_BIN") or "adb"


def _run_adb(serial: str, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    cmd = [_default_adb_bin(), "-s", serial, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _default_shell(serial: str, command: str, timeout: float) -> str:
    r = _run_adb(serial, "shell", command, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "adb shell failed").strip())
    return (r.stdout or "").strip()


def _default_genfarmer_shell(serial: str, inner: str, timeout: float) -> str:
    """Run a command as root via genfarmer -c '<inner>'."""
    quoted = "'" + inner.replace("'", "'\\''") + "'"
    full = f"{GENFARMER_BIN} -c {quoted}"
    return _default_shell(serial, full, timeout)


def _default_pull(serial: str, remote: str, local: str, timeout: float) -> None:
    r = _run_adb(serial, "pull", remote, local, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "adb pull failed").strip())


def _default_push(serial: str, local: str, remote: str, timeout: float) -> None:
    r = _run_adb(serial, "push", local, remote, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "adb push failed").strip())


def default_backup_client(
    *,
    backup_root: Path | None = None,
    backup_paths: list[str] | None = None,
    timeout: float = 120.0,
) -> AppBackupClient:
    """Create an AppBackupClient with real adb + genfarmer transports."""
    return AppBackupClient(
        shell=_default_shell,
        genfarmer_shell=_default_genfarmer_shell,
        pull=_default_pull,
        push=_default_push,
        backup_root=backup_root,
        backup_paths=backup_paths,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_serial(serial: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", serial)
