"""Tests for the GenFarmer ChangeDevice client (mocked I/O — no device/network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.genfarmer.changedevice import (
    CHANGE_CMD,
    CLEAR_CHANGE_CMD,
    ChangeDeviceClient,
    ChangeDeviceError,
    DeviceProfile,
)


# --- fakes -----------------------------------------------------------------
class FakeShell:
    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, serial: str, command: str) -> str:
        self.calls.append((serial, command))
        for key, val in self.responses.items():
            if key in command:
                return val
        return ""


class FakePush:
    def __init__(self):
        self.pushed: list[tuple[str, str, str]] = []

    async def __call__(self, serial: str, local: str, remote: str) -> None:
        self.pushed.append((serial, remote, Path(local).read_text(encoding="utf-8")))


class FakeState:
    def __init__(self, seq):
        self.seq = list(seq)

    async def __call__(self, serial: str) -> str:
        return self.seq.pop(0) if self.seq else "offline"


class FakeHttp:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    async def __call__(self, url: str):
        self.calls += 1
        return self.bodies.pop(0) if self.bodies else (200, '{"success":true,"data":{}}')


def _rand_body(release, model="SM-TEST"):
    return (200, json.dumps({"success": True, "data": {
        "dmMIN.model": model,
        "dmMIN.version.release": str(release),
        "dmMIN.sdk": "31",
        "dmMIN.fingerprint": f"samsung/x/x:{release}/BUILD/INC:user/release-keys",
    }}))


_READY = {"genfarmer.activated": "1", "init.svc.genfarmer_command": "running",
          "ls /system/bin/genfarmer": "-rwsr-x--- root /system/bin/genfarmer"}


def _client(shell=None, push=None, state=None, http=None):
    return ChangeDeviceClient(
        shell=shell or FakeShell(),
        push=push or FakePush(),
        state=state or FakeState([]),
        http_get=http or FakeHttp([]),
    )


# --- DeviceProfile ---------------------------------------------------------
def test_props_roundtrip_and_key_order():
    text = ("dmMIN.model=SM-G781B\n# comment\n\n"
            "dmMIN.fingerprint=samsung/r8qxxx/r8q:12/SP1A/INC:user/release-keys\n"
            "dmMIN.version.release=12\ndmMIN.zzz_extra=keep\ndmMIN.blank=\n")
    p = DeviceProfile.from_props(text)
    assert p.model == "SM-G781B"
    assert p.android_major == 12
    assert "dmMIN.blank" not in p.fields  # empty values dropped
    out = p.to_props().splitlines()
    # canonical order: fingerprint precedes model; trailing extras sorted last
    assert out[0].startswith("dmMIN.fingerprint=")
    assert "dmMIN.model=SM-G781B" in out
    assert out[-1] == "dmMIN.zzz_extra=keep"


def test_from_json_and_mapping_drop_empty():
    p = DeviceProfile.from_json('{"dmMIN.model":"X","dmMIN.empty":"","dmMIN.sdk":31}')
    assert p.model == "X" and p.fields["dmMIN.sdk"] == "31"
    assert "dmMIN.empty" not in p.fields


def test_android_major_parsing():
    assert DeviceProfile({"dmMIN.version.release": "12"}).android_major == 12
    assert DeviceProfile({"dmMIN.version.release": "8.0.0"}).android_major == 8
    assert DeviceProfile({}).android_major == 0


# --- prepare_props (serial keep/generate) ----------------------------------
def test_prepare_props_keeps_saved_serial_on_restore():
    saved = DeviceProfile({"dmMIN.model": "SM-G781B", "dmMIN.version.release": "12",
                           "dmMIN.serialno": "ce8fb4b49e27b7c763ed",
                           "dmMIN.android_id": "cffc97e303863479"})
    text = _client().prepare_props(saved, "1.2.3.4:5555", keep_serial=True)
    assert "dmMIN.serialno=ce8fb4b49e27b7c763ed" in text   # exact restore
    assert "dmMIN.android_id=cffc97e303863479" in text


def test_prepare_props_generates_serial_for_new_account():
    fresh = DeviceProfile({"dmMIN.model": "SM-G781B", "dmMIN.version.release": "12"})  # no serial
    text = _client().prepare_props(fresh, "1.2.3.4:5555", keep_serial=True)
    line = [ln for ln in text.splitlines() if ln.startswith("dmMIN.serialno=")][0]
    serial = line.split("=", 1)[1]
    assert serial.startswith("ce") and len(serial) == 20 and serial != "ce8fb4b49e27b7c763ed"
    assert "dmMIN.locale=en-US" in text  # default filled


def test_prepare_props_keep_serial_false_regenerates():
    saved = DeviceProfile({"dmMIN.model": "X", "dmMIN.serialno": "ce0000000000000000ab"})
    text = _client().prepare_props(saved, "1.2.3.4:5555", keep_serial=False)
    assert "dmMIN.serialno=ce0000000000000000ab" not in text


# --- fetch_random (client-side Android filter) -----------------------------
def test_fetch_random_filters_min_android():
    http = FakeHttp([_rand_body(10), _rand_body(11), _rand_body(12, "SM-G781B")])
    profile = asyncio.run(_client(http=http).fetch_random(min_android=12, max_tries=5))
    assert profile.android_major == 12 and profile.model == "SM-G781B"
    assert http.calls == 3  # skipped the 10 and 11


def test_fetch_random_no_filter_takes_first():
    http = FakeHttp([_rand_body(10)])
    profile = asyncio.run(_client(http=http).fetch_random())
    assert profile.android_major == 10 and http.calls == 1


def test_fetch_random_exhausts_raises():
    http = FakeHttp([_rand_body(10)] * 3)
    with pytest.raises(ChangeDeviceError):
        asyncio.run(_client(http=http).fetch_random(min_android=12, max_tries=3))


def test_fetch_random_http_error_raises():
    http = FakeHttp([(500, "boom")])
    with pytest.raises(ChangeDeviceError):
        asyncio.run(_client(http=http).fetch_random())


# --- capture ---------------------------------------------------------------
def test_capture_overrides_serial_and_android_id_with_live_values():
    dump = ("[dmMIN.model]: [SM-G781B]\n"
            "[dmMIN.fingerprint]: [samsung/r8qxxx/r8q:12/SP1A/INC:user/release-keys]\n"
            "[dmMIN.serialno]: [STALE_DMMIN_SERIAL]\n"
            "[ro.serialno]: [ce8fb4b49e27b7c763ed]\n"
            "[ro.product.model]: [SM-G781B]\n")
    shell = FakeShell({"getprop": dump, "settings get secure android_id": "cffc97e303863479\n"})
    profile = asyncio.run(_client(shell=shell).capture("1.2.3.4:5555"))
    assert profile.serialno == "ce8fb4b49e27b7c763ed"      # live ro.serialno wins
    assert profile.android_id == "cffc97e303863479"        # live settings value
    assert profile.model == "SM-G781B"


def test_capture_falls_back_to_ro_props_when_no_dmmin():
    dump = ("[ro.product.model]: [Pixel 6]\n[ro.product.device]: [oriole]\n"
            "[ro.build.fingerprint]: [google/oriole/oriole:12/SQ3A/INC:user/release-keys]\n"
            "[ro.serialno]: [988e9032304144305a30]\n[ro.build.version.release]: [12]\n")
    shell = FakeShell({"getprop": dump, "settings get secure android_id": "abc123\n"})
    profile = asyncio.run(_client(shell=shell).capture("1.2.3.4:5555"))
    assert profile.model == "Pixel 6" and profile.serialno == "988e9032304144305a30"
    assert profile.android_major == 12


# --- apply -----------------------------------------------------------------
def test_apply_pushes_props_and_triggers_change():
    shell = FakeShell(dict(_READY))
    push = FakePush()
    saved = DeviceProfile({"dmMIN.model": "SM-G781B", "dmMIN.version.release": "12",
                           "dmMIN.serialno": "ce8fb4b49e27b7c763ed"})
    asyncio.run(_client(shell=shell, push=push).apply("1.2.3.4:5555", saved))
    # staged the exact serial to the canonical remote path
    assert push.pushed and "dmMIN.serialno=ce8fb4b49e27b7c763ed" in push.pushed[0][2]
    assert push.pushed[0][1] == "/data/local/tmp/.genfarmer_props"
    # triggered the plain change command (not wipe)
    assert any(f"setprop genfarmer.command {CHANGE_CMD}" in c for _, c in shell.calls)
    assert not any(CLEAR_CHANGE_CMD in c for _, c in shell.calls)


def test_apply_clear_data_uses_wipe_command():
    shell = FakeShell(dict(_READY))
    saved = DeviceProfile({"dmMIN.model": "X", "dmMIN.serialno": "ce11"})
    asyncio.run(_client(shell=shell, push=FakePush()).apply("s", saved, clear_data=True))
    assert any(f"setprop genfarmer.command {CLEAR_CHANGE_CMD}" in c for _, c in shell.calls)


def test_apply_refuses_when_not_ready():
    shell = FakeShell({})  # ready() -> False
    with pytest.raises(ChangeDeviceError):
        asyncio.run(_client(shell=shell, push=FakePush()).apply("s", DeviceProfile({"dmMIN.model": "X"})))


# --- ready / wait_reconnect ------------------------------------------------
def test_ready_true_when_rom_present():
    assert asyncio.run(_client(shell=FakeShell(dict(_READY))).ready("s")) is True


def test_ready_false_when_helper_missing():
    shell = FakeShell({"genfarmer.activated": "1", "init.svc.genfarmer_command": "running"})
    assert asyncio.run(_client(shell=shell).ready("s")) is False


def test_wait_reconnect_returns_true_when_device_comes_back():
    state = FakeState(["offline", "offline", "device"])
    ok = asyncio.run(_client(state=state).wait_reconnect("s", timeout=5, poll=0))
    assert ok is True


def test_wait_reconnect_times_out():
    ok = asyncio.run(_client(state=FakeState(["offline"])).wait_reconnect("s", timeout=0, poll=0))
    assert ok is False
