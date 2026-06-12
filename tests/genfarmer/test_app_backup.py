"""Unit tests for src.genfarmer.app_backup — backup/restore via genfarmer root shell."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.genfarmer.app_backup import (
    AppBackupClient,
    BackupManifest,
    BackupResult,
    RestoreResult,
)


# ---------------------------------------------------------------------------
# Fake I/O
# ---------------------------------------------------------------------------


class FakeIO:
    """Records calls and returns canned responses."""

    def __init__(self):
        self.shell_log: list[tuple[str, str]] = []
        self.gf_log: list[tuple[str, str]] = []
        self.pull_log: list[tuple[str, str, str]] = []
        self.push_log: list[tuple[str, str, str]] = []
        self.shell_responses: dict[str, str] = {}
        self.gf_responses: dict[str, str] = {}
        self.installed_packages: set[str] = {"com.instagram.android"}
        self.uid = 10227
        self.existing_dirs: set[str] = {"shared_prefs", "databases", "files"}

    def shell(self, serial: str, command: str, timeout: float) -> str:
        self.shell_log.append((serial, command))
        # pm path check
        if "pm path" in command:
            pkg = command.split()[-1]
            if pkg in self.installed_packages:
                return f"package:/data/app/{pkg}/base.apk"
            raise RuntimeError("not installed")
        # pm list packages -U
        if "pm list packages -U" in command:
            return f"package:com.instagram.android uid:{self.uid}"
        # am force-stop
        if "am force-stop" in command:
            return ""
        return self.shell_responses.get(command, "")

    def gf_shell(self, serial: str, inner: str, timeout: float) -> str:
        self.gf_log.append((serial, inner))
        # test -e checks
        if "test -e" in inner:
            for d in self.existing_dirs:
                if d in inner:
                    return "YES"
            return "NO"
        # tar commands
        if "tar -czf" in inner:
            return ""
        if "tar -xzf" in inner:
            return ""
        # chown
        if "chown" in inner:
            return ""
        # restorecon
        if "restorecon" in inner:
            return ""
        # rm
        if "rm -f" in inner:
            return ""
        return self.gf_responses.get(inner, "")

    def pull(self, serial: str, remote: str, local: str, timeout: float) -> None:
        self.pull_log.append((serial, remote, local))
        # Create the file so the client can stat it
        Path(local).write_bytes(b"fake tar content " * 100)

    def push(self, serial: str, local: str, remote: str, timeout: float) -> None:
        self.push_log.append((serial, local, remote))


def make_client(io: FakeIO, tmp_path: Path) -> AppBackupClient:
    return AppBackupClient(
        shell=io.shell,
        genfarmer_shell=io.gf_shell,
        pull=io.pull,
        push=io.push,
        backup_root=tmp_path / "backups",
    )


# ---------------------------------------------------------------------------
# Backup tests
# ---------------------------------------------------------------------------


class TestBackup:
    def test_successful_backup(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)

        result = client.backup("192.168.1.1:5555", "com.instagram.android", label="test1")

        assert result.ok
        assert result.backup_dir is not None
        assert result.backup_dir.exists()
        assert result.archive_size_bytes > 0
        assert result.manifest is not None
        assert result.manifest.package == "com.instagram.android"
        assert result.manifest.uid == 10227
        assert set(result.manifest.rel_paths) == {"shared_prefs", "databases", "files"}

        # Manifest file written
        mf_path = result.backup_dir / "manifest.json"
        assert mf_path.exists()
        mf = json.loads(mf_path.read_text())
        assert mf["package"] == "com.instagram.android"
        assert mf["uid"] == 10227

    def test_backup_not_installed(self, tmp_path: Path):
        io = FakeIO()
        io.installed_packages.clear()
        client = make_client(io, tmp_path)

        result = client.backup("192.168.1.1:5555", "com.instagram.android")

        assert not result.ok
        assert "not installed" in result.error.lower()

    def test_backup_no_data_dirs(self, tmp_path: Path):
        io = FakeIO()
        io.existing_dirs.clear()
        client = make_client(io, tmp_path)

        result = client.backup("192.168.1.1:5555", "com.instagram.android")

        assert not result.ok
        assert "no data dirs" in result.error.lower()

    def test_backup_custom_out_dir(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        custom_dir = tmp_path / "custom_backup"

        result = client.backup(
            "192.168.1.1:5555", "com.instagram.android", out_dir=custom_dir
        )

        assert result.ok
        assert result.backup_dir == custom_dir
        assert (custom_dir / "manifest.json").exists()

    def test_backup_force_stop_called(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)

        client.backup("192.168.1.1:5555", "com.instagram.android")

        force_stops = [
            cmd for _, cmd in io.shell_log if "force-stop" in cmd
        ]
        assert len(force_stops) == 1
        assert "com.instagram.android" in force_stops[0]

    def test_backup_no_force_stop(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)

        client.backup("192.168.1.1:5555", "com.instagram.android", force_stop=False)

        force_stops = [cmd for _, cmd in io.shell_log if "force-stop" in cmd]
        assert len(force_stops) == 0

    def test_backup_tar_command_uses_genfarmer(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)

        client.backup("192.168.1.1:5555", "com.instagram.android")

        tar_cmds = [cmd for _, cmd in io.gf_log if "tar -czf" in cmd]
        assert len(tar_cmds) == 1
        assert "/data/data/com.instagram.android/shared_prefs" in tar_cmds[0]

    def test_backup_pulls_archive(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)

        client.backup("192.168.1.1:5555", "com.instagram.android")

        assert len(io.pull_log) == 1
        serial, remote, local = io.pull_log[0]
        assert serial == "192.168.1.1:5555"
        assert "backup.tar.gz" in remote
        assert "data.tgz" in local

    def test_backup_cleans_remote_tar(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)

        client.backup("192.168.1.1:5555", "com.instagram.android")

        rm_cmds = [cmd for _, cmd in io.gf_log if "rm -f" in cmd]
        assert len(rm_cmds) == 1


# ---------------------------------------------------------------------------
# Restore tests
# ---------------------------------------------------------------------------


class TestRestore:
    def _make_backup_dir(self, tmp_path: Path) -> Path:
        """Create a fake backup directory with manifest + archive."""
        bdir = tmp_path / "backup_test"
        bdir.mkdir()
        manifest = {
            "serial": "192.168.1.1:5555",
            "package": "com.instagram.android",
            "uid": 10227,
            "rel_paths": ["shared_prefs", "databases"],
            "archive_name": "data.tgz",
            "created_at": "2026-06-11T10:00:00",
            "app_data_base": "/data/data/com.instagram.android",
        }
        (bdir / "manifest.json").write_text(json.dumps(manifest))
        (bdir / "data.tgz").write_bytes(b"fake archive data")
        return bdir

    def test_successful_restore(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        bdir = self._make_backup_dir(tmp_path)

        result = client.restore("192.168.1.1:5555", "com.instagram.android", bdir)

        assert result.ok

    def test_restore_pushes_archive(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        bdir = self._make_backup_dir(tmp_path)

        client.restore("192.168.1.1:5555", "com.instagram.android", bdir)

        assert len(io.push_log) == 1
        _, local, remote = io.push_log[0]
        assert "data.tgz" in local
        assert "backup.tar.gz" in remote

    def test_restore_extracts_and_chowns(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        bdir = self._make_backup_dir(tmp_path)

        client.restore("192.168.1.1:5555", "com.instagram.android", bdir)

        tar_cmds = [cmd for _, cmd in io.gf_log if "tar -xzf" in cmd]
        chown_cmds = [cmd for _, cmd in io.gf_log if "chown" in cmd]
        restorecon_cmds = [cmd for _, cmd in io.gf_log if "restorecon" in cmd]
        assert len(tar_cmds) == 1
        assert len(chown_cmds) == 1
        assert "10227:10227" in chown_cmds[0]
        assert len(restorecon_cmds) == 1

    def test_restore_no_manifest(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        bdir = tmp_path / "empty"
        bdir.mkdir()

        result = client.restore("192.168.1.1:5555", "com.instagram.android", bdir)

        assert not result.ok
        assert "manifest" in result.error.lower()

    def test_restore_not_installed(self, tmp_path: Path):
        io = FakeIO()
        io.installed_packages.clear()
        client = make_client(io, tmp_path)
        bdir = self._make_backup_dir(tmp_path)

        result = client.restore("192.168.1.1:5555", "com.instagram.android", bdir)

        assert not result.ok
        assert "not installed" in result.error.lower()

    def test_restore_force_stop(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        bdir = self._make_backup_dir(tmp_path)

        client.restore("192.168.1.1:5555", "com.instagram.android", bdir)

        force_stops = [cmd for _, cmd in io.shell_log if "force-stop" in cmd]
        assert len(force_stops) == 1

    def test_restore_no_restorecon(self, tmp_path: Path):
        io = FakeIO()
        client = make_client(io, tmp_path)
        bdir = self._make_backup_dir(tmp_path)

        client.restore(
            "192.168.1.1:5555", "com.instagram.android", bdir, restorecon=False
        )

        restorecon_cmds = [cmd for _, cmd in io.gf_log if "restorecon" in cmd]
        assert len(restorecon_cmds) == 0


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestManifest:
    def test_roundtrip(self):
        m = BackupManifest(
            serial="10.0.0.1:5555",
            package="com.test.app",
            uid=12345,
            rel_paths=["shared_prefs", "databases"],
            archive_name="data.tgz",
            created_at="2026-06-11T10:00:00",
            app_data_base="/data/data/com.test.app",
        )
        d = m.to_dict()
        m2 = BackupManifest.from_dict(d)
        assert m2.serial == m.serial
        assert m2.package == m.package
        assert m2.uid == m.uid
        assert m2.rel_paths == m.rel_paths
        assert m2.archive_name == m.archive_name
