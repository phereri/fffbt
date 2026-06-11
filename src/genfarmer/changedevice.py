"""GenFarmer **ChangeDevice** — rotate / capture / restore a phone's device identity.

Mechanism (reverse-engineered from ``app.asar`` + the team's GenBR tooling, and
validated end-to-end 2026-06-11): a device profile is a set of ``dmMIN.*``
properties. To apply one you write it to ``/data/local/tmp/.genfarmer_props`` on
the phone and then ``setprop genfarmer.command change_device`` — the on-device
GenFarmer ROM helper (``/system/bin/genfarmer``, setuid root; requires
``genfarmer.activated=1`` and ``init.svc.genfarmer_command=running``) consumes the
staged props, applies the new identity and reboots (~90 s).

Three operations, all proven on the fleet:

* :meth:`ChangeDeviceClient.fetch_random` — pull a random profile from the local
  GenFarmer API (``GET /devices/random``); optionally loop until the profile is
  Android ``>= min_android`` (the pool is only ~1/6 twelve-plus and the server
  ignores filter params, so we filter client-side on ``version.release``).
* :meth:`ChangeDeviceClient.apply` — enrich, stage and trigger. For a **new**
  account use a fresh random profile (a new ``serialno`` is generated). For
  **returning** to an existing account apply its **saved** profile with
  ``keep_serial=True`` (the saved ``serialno`` is preserved verbatim).
* :meth:`ChangeDeviceClient.capture` — read the device's current identity into a
  :class:`DeviceProfile` to persist per account.

Round-trip verified: ``capture`` → ``apply(random)`` → ``apply(saved)`` restores
**every** identity field exactly (model / fingerprint / serialno / android_id /
sdk); only ``ro.boot_id`` differs (a per-boot UUID, not part of the identity).

I/O (adb shell/push, reachability, HTTP) is injected so the pure profile logic is
unit-testable without a device. :func:`default_client` wires the real adb +
``urllib`` transports for running on the GenFarmer host.

See ``docs/runbooks/changedevice.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping

API_BASE = "http://127.0.0.1:55554"
PROPS_REMOTE_PATH = "/data/local/tmp/.genfarmer_props"
CHANGE_CMD = "change_device"          # genfarmer.command for a profile swap
CLEAR_CHANGE_CMD = "wipe_data_change"  # ... + wipe app data (resets android_id too)

# Canonical serialization order for the dmMIN.* keys (matches GenFarmer's own
# ``/devices/random`` payload). Any extra dmMIN.* keys are appended, sorted.
_PROFILE_KEY_ORDER: tuple[str, ...] = (
    "dmMIN.fingerprint",
    "dmMIN.device",
    "dmMIN.name",
    "dmMIN.brand",
    "dmMIN.model",
    "dmMIN.manufacturer",
    "dmMIN.build.id",
    "dmMIN.display.id",
    "dmMIN.bootloader",
    "dmMIN.baseband",
    "dmMIN.hardware",
    "dmMIN.version.release",
    "dmMIN.locale",
    "dmMIN.simslotcount",
    "dmMIN.platform",
    "dmMIN.chipname",
    "dmMIN.board",
    "dmMIN.sdk",
    "dmMIN.date",
    "dmMIN.serialno",
    "dmMIN.android_id",
)

# ro.* / settings source -> dmMIN.* key, used by ``capture`` to reconstruct a
# faithful profile from a live device (when GenFarmer's own dmMIN props are absent).
_RO_TO_DMMIN: tuple[tuple[str, str], ...] = (
    ("ro.build.fingerprint", "dmMIN.fingerprint"),
    ("ro.product.device", "dmMIN.device"),
    ("ro.product.name", "dmMIN.name"),
    ("ro.product.brand", "dmMIN.brand"),
    ("ro.product.model", "dmMIN.model"),
    ("ro.product.manufacturer", "dmMIN.manufacturer"),
    ("ro.build.id", "dmMIN.build.id"),
    ("ro.build.display.id", "dmMIN.display.id"),
    ("ro.bootloader", "dmMIN.bootloader"),
    ("ro.hardware", "dmMIN.hardware"),
    ("ro.board.platform", "dmMIN.platform"),
    ("ro.product.board", "dmMIN.board"),
    ("ro.build.version.release", "dmMIN.version.release"),
    ("ro.build.version.sdk", "dmMIN.sdk"),
    ("ro.build.version.security_patch", "dmMIN.security_patch"),
    ("ro.build.version.incremental", "dmMIN.incremental"),
    ("ro.build.type", "dmMIN.type"),
    ("ro.build.flavor", "dmMIN.flavor"),
    ("ro.serialno", "dmMIN.serialno"),
)

_GETPROP_LINE = re.compile(r"^\[(?P<key>[^\]]+)\]:\s*\[(?P<val>.*)\]$")


class ChangeDeviceError(RuntimeError):
    """A ChangeDevice operation failed (device not ready, API error, ...)."""


def _random_serial() -> str:
    """A realistic device serial (Samsung-style ``ce`` + hex, 20 chars)."""
    return "ce" + secrets.token_hex(9)


def _random_android_id() -> str:
    return secrets.token_hex(8)


def _now_date() -> str:
    # GenFarmer stores a human-readable boot date; format is not load-bearing.
    return time.strftime("%a, %b %d, %Y, %I:%M:%S %p GMT+7", time.gmtime())


@dataclass
class DeviceProfile:
    """A GenFarmer device identity = a bag of ``dmMIN.*`` properties.

    Construct from the ``/devices/random`` payload, a saved ``.props`` / ``.json``
    file, or a live device (:meth:`ChangeDeviceClient.capture`). Empty values are
    dropped on serialization.
    """

    fields: dict[str, str]

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "DeviceProfile":
        return cls({str(k): str(v) for k, v in data.items() if str(v).strip()})

    @classmethod
    def from_props(cls, text: str) -> "DeviceProfile":
        """Parse ``key=value`` lines (``#`` comments and blanks ignored)."""
        out: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if v.strip():
                out[k.strip()] = v.strip()
        return cls(out)

    @classmethod
    def from_json(cls, text: str) -> "DeviceProfile":
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("device profile JSON must be an object")
        return cls.from_mapping(raw)

    @classmethod
    def load(cls, path: str | Path) -> "DeviceProfile":
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        return cls.from_json(text) if p.suffix.lower() == ".json" else cls.from_props(text)

    # -- serialization ------------------------------------------------------
    def to_props(self) -> str:
        """``dmMIN.key=value`` lines, canonical key order first."""
        lines: list[str] = []
        seen: set[str] = set()
        for key in _PROFILE_KEY_ORDER:
            val = self.fields.get(key, "").strip()
            if val:
                lines.append(f"{key}={val}")
                seen.add(key)
        for key in sorted(self.fields):
            if key.startswith("dmMIN.") and key not in seen and self.fields[key].strip():
                lines.append(f"{key}={self.fields[key].strip()}")
        if not lines:
            raise ValueError("empty device profile — nothing to serialize")
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps(self.fields, indent=1, ensure_ascii=False)

    def with_overrides(self, **dm_fields: str) -> "DeviceProfile":
        merged = dict(self.fields)
        merged.update({k: v for k, v in dm_fields.items()})
        return DeviceProfile(merged)

    # -- accessors ----------------------------------------------------------
    def _get(self, key: str) -> str:
        return self.fields.get(key, "")

    @property
    def model(self) -> str:
        return self._get("dmMIN.model")

    @property
    def fingerprint(self) -> str:
        return self._get("dmMIN.fingerprint")

    @property
    def serialno(self) -> str:
        return self._get("dmMIN.serialno")

    @property
    def android_id(self) -> str:
        return self._get("dmMIN.android_id")

    @property
    def android_release(self) -> str:
        return self._get("dmMIN.version.release")

    @property
    def android_major(self) -> int:
        m = re.match(r"\s*(\d+)", self.android_release)
        return int(m.group(1)) if m else 0

    def summary(self) -> str:
        return (
            f"{self.model or '?'} / Android {self.android_release or '?'} "
            f"(sdk {self._get('dmMIN.sdk') or '?'}) serial={self.serialno or '?'}"
        )


# Injectable I/O — defaults wired by ``default_client``.
Shell = Callable[[str, str], Awaitable[str]]          # (serial, command) -> stdout
Push = Callable[[str, str, str], Awaitable[None]]      # (serial, local, remote)
State = Callable[[str], Awaitable[str]]                # (serial) -> "device"/"offline"/...
HttpGet = Callable[[str], Awaitable[tuple[int, str]]]  # (url) -> (status, body)


class ChangeDeviceClient:
    """Drive GenFarmer ChangeDevice over adb + the local REST API."""

    def __init__(
        self,
        *,
        shell: Shell,
        push: Push,
        state: State,
        http_get: HttpGet,
        api_base: str = API_BASE,
        props_remote_path: str = PROPS_REMOTE_PATH,
    ) -> None:
        self._shell = shell
        self._push = push
        self._state = state
        self._http_get = http_get
        self._api_base = api_base.rstrip("/")
        self._remote = props_remote_path

    # -- readiness ----------------------------------------------------------
    async def ready(self, serial: str) -> bool:
        """True when the GenFarmer ROM helper is present + activated on ``serial``."""
        activated = (await self._shell(serial, "getprop genfarmer.activated")).strip()
        svc = (await self._shell(serial, "getprop init.svc.genfarmer_command")).strip()
        helper = await self._shell(serial, "ls /system/bin/genfarmer 2>/dev/null || true")
        return activated == "1" and svc == "running" and "genfarmer" in helper

    # -- profile sources ----------------------------------------------------
    async def fetch_random(self, *, min_android: int | None = None, max_tries: int = 40) -> DeviceProfile:
        """Random profile from ``GET /devices/random``; loop until Android >= ``min_android``."""
        last: DeviceProfile | None = None
        for _ in range(max(1, max_tries)):
            status, body = await self._http_get(f"{self._api_base}/devices/random")
            if status != 200:
                raise ChangeDeviceError(f"GET /devices/random -> HTTP {status}: {body[:200]}")
            payload = json.loads(body)
            if not payload.get("success"):
                raise ChangeDeviceError(f"/devices/random error: {payload}")
            last = DeviceProfile.from_mapping(payload.get("data") or {})
            if min_android is None or last.android_major >= min_android:
                return last
        raise ChangeDeviceError(
            f"no Android>={min_android} profile in {max_tries} tries (pool is mostly older)"
        )

    async def capture(self, serial: str) -> DeviceProfile:
        """Faithful profile of the device's CURRENT identity (for per-account save).

        Prefers GenFarmer's own ``dmMIN.*`` props; falls back to ``ro.*``. Always
        overrides ``serialno``/``android_id`` with the live ``ro.serialno`` /
        ``settings.secure.android_id`` so a later restore is exact.
        """
        dump = await self._shell(serial, "getprop")
        props: dict[str, str] = {}
        for line in dump.splitlines():
            m = _GETPROP_LINE.match(line.strip())
            if m:
                props[m.group("key")] = m.group("val")

        fields = {k: v for k, v in props.items() if k.startswith("dmMIN.") and v.strip()}
        for ro_key, dm_key in _RO_TO_DMMIN:
            if not fields.get(dm_key) and props.get(ro_key, "").strip():
                fields[dm_key] = props[ro_key].strip()

        real_serial = props.get("ro.serialno", "").strip()
        if real_serial:
            fields["dmMIN.serialno"] = real_serial
        android_id = (await self._shell(serial, "settings get secure android_id")).strip()
        if android_id and android_id != "null":
            fields["dmMIN.android_id"] = android_id
        if not fields:
            raise ChangeDeviceError(f"could not read any identity props from {serial}")
        return DeviceProfile(fields)

    async def identity(self, serial: str) -> dict[str, str]:
        """Compact live ``ro.*`` identity for before/after diffs."""
        keys = (
            "ro.product.model", "ro.product.device", "ro.build.version.release",
            "ro.build.version.sdk", "ro.build.fingerprint", "ro.serialno",
        )
        out: dict[str, str] = {}
        for k in keys:
            out[k] = (await self._shell(serial, f"getprop {k}")).strip()
        out["android_id"] = (await self._shell(serial, "settings get secure android_id")).strip()
        return out

    # -- apply --------------------------------------------------------------
    def prepare_props(self, profile: DeviceProfile, serial: str, *, keep_serial: bool = True) -> str:
        """Enrich a profile and serialize it to the staged ``.props`` text.

        ``keep_serial=True`` preserves an existing ``dmMIN.serialno`` (restore of a
        saved account); otherwise — or when the profile has none (a fresh random
        profile) — a new realistic serial is generated (a new account). ``locale``,
        ``android_id`` and ``date`` are filled only when missing.
        """
        fields = dict(profile.fields)
        fields.setdefault("dmMIN.locale", "en-US")
        if not keep_serial or not fields.get("dmMIN.serialno", "").strip():
            fields["dmMIN.serialno"] = _random_serial()
        fields.setdefault("dmMIN.android_id", _random_android_id())
        fields.setdefault("dmMIN.date", _now_date())
        return DeviceProfile(fields).to_props()

    async def apply(
        self,
        serial: str,
        profile: DeviceProfile,
        *,
        clear_data: bool = False,
        keep_serial: bool = True,
        require_ready: bool = True,
    ) -> str:
        """Stage ``profile`` and trigger the change. Returns the props text applied.

        The phone reboots shortly after; use :meth:`wait_reconnect`. ``clear_data``
        wipes app data (and rotates the real ``android_id``) via
        ``wipe_data_change``; the default keeps apps/data.
        """
        if require_ready and not await self.ready(serial):
            raise ChangeDeviceError(
                f"{serial} is not GenFarmer-ready (genfarmer.activated / "
                f"init.svc.genfarmer_command / /system/bin/genfarmer)"
            )
        props_text = self.prepare_props(profile, serial, keep_serial=keep_serial)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".props", delete=False) as tf:
            tf.write(props_text)
            local = tf.name
        try:
            await self._push(serial, local, self._remote)
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass
        await self._shell(serial, f"chmod 660 {self._remote} 2>/dev/null || true")
        command = CLEAR_CHANGE_CMD if clear_data else CHANGE_CMD
        await self._shell(serial, f"setprop genfarmer.command {command}")
        return props_text

    # -- reconnect ----------------------------------------------------------
    async def wait_reconnect(self, serial: str, *, timeout: float = 300.0, poll: float = 5.0) -> bool:
        """Poll until the phone is back on adb after the change-reboot (~90 s)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if (await self._state(serial)) == "device":
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll)
        return False


def default_client(
    *,
    api_base: str = API_BASE,
    props_remote_path: str = PROPS_REMOTE_PATH,
    adb_path: str | None = None,
) -> ChangeDeviceClient:
    """A client wired to the real adb binary + ``urllib`` (run on the GenFarmer host)."""
    adb = adb_path or os.environ.get("ADB_PATH") or os.environ.get("ADB_BIN") or "adb"

    def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    async def shell(serial: str, command: str) -> str:
        proc = await asyncio.to_thread(_run, [adb, "-s", serial, "shell", command], 25.0)
        return proc.stdout or ""

    async def push(serial: str, local: str, remote: str) -> None:
        proc = await asyncio.to_thread(_run, [adb, "-s", serial, "push", local, remote], 120.0)
        if proc.returncode != 0:
            raise ChangeDeviceError((proc.stderr or proc.stdout or "adb push failed").strip())

    async def state(serial: str) -> str:
        try:
            await asyncio.to_thread(_run, [adb, "connect", serial], 12.0)
            proc = await asyncio.to_thread(_run, [adb, "-s", serial, "get-state"], 10.0)
            return (proc.stdout or "").strip()
        except subprocess.TimeoutExpired:
            return "offline"

    import urllib.request

    async def http_get(url: str) -> tuple[int, str]:
        def _get() -> tuple[int, str]:
            with urllib.request.urlopen(url, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        return await asyncio.to_thread(_get)

    return ChangeDeviceClient(
        shell=shell, push=push, state=state, http_get=http_get,
        api_base=api_base, props_remote_path=props_remote_path,
    )


__all__ = [
    "API_BASE",
    "PROPS_REMOTE_PATH",
    "CHANGE_CMD",
    "CLEAR_CHANGE_CMD",
    "ChangeDeviceClient",
    "ChangeDeviceError",
    "DeviceProfile",
    "default_client",
]
