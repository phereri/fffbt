"""Tests for the device fingerprint snapshot (``fingerprint.py``).

No real device — a fake async ``shell`` returns canned ADB output keyed by
command substring. Verifies prop parsing, settings/wm/file captures, the
fp_* -> CSV mapping, raw getprop passthrough, and resilience to failing calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.registration.fingerprint import (
    FingerprintSnapshot,
    snapshot_fingerprint,
)
from src.registration.output import CSV_COLUMNS


_GETPROP_DUMP = "\n".join(
    [
        "[ro.product.model]: [Pixel 7]",
        "[ro.product.brand]: [google]",
        "[ro.product.manufacturer]: [Google]",
        "[ro.product.name]: [panther]",
        "[ro.product.device]: [panther]",
        "[ro.build.fingerprint]: [google/panther/panther:14/UP1A.231005.007/123:user/release-keys]",
        "[ro.build.id]: [UP1A.231005.007]",
        "[ro.build.version.release]: [14]",
        "[ro.build.version.sdk]: [34]",
        "[ro.serialno]: [1A2B3C4D]",
        "[ro.ril.imei]: [351234567890123]",
        "[gsm.sim.imsi]: [250991234567890]",
        "[gsm.sim.operator.alpha]: [Beeline]",
        "[gsm.sim.operator.numeric]: [25099]",
        "[persist.sys.locale]: [en-US]",
        "[persist.sys.timezone]: [Europe/Moscow]",
    ]
)


class _FakeShell:
    """Async shell stub. Maps a command substring -> canned stdout."""

    def __init__(self, responses: dict[str, str], fail: set[str] | None = None):
        self._responses = responses
        self._fail = fail or set()
        self.calls: list[str] = []

    async def __call__(self, serial: str, cmd: str, timeout: int = 60) -> str:
        self.calls.append(cmd)
        for needle in self._fail:
            if needle in cmd:
                raise RuntimeError(f"adb fail for {cmd}")
        for needle, out in self._responses.items():
            if needle in cmd:
                return out
        return ""


def _default_responses() -> dict[str, str]:
    return {
        "getprop": _GETPROP_DUMP,
        "android_id": "abcd1234ef567890\n",
        "advertising_id": "11111111-2222-3333-4444-555555555555\n",
        "boot_id": "deadbeef-0000-1111-2222-333344445555\n",
        "wlan0/address": "aa:bb:cc:dd:ee:ff\n",
        "wm size": "Physical size: 1080x2400\n",
        "wm density": "Physical density: 420\n",
        "ip ": "100.64.0.5\n",
    }


def _run(serial="100.64.0.5:5555", **kw):
    shell = kw.pop("shell", None) or _FakeShell(_default_responses())
    snap = asyncio.run(snapshot_fingerprint(serial, shell=shell, **kw))
    return snap, shell


class TestSnapshotFields:
    def test_returns_snapshot(self):
        snap, _ = _run()
        assert isinstance(snap, FingerprintSnapshot)

    def test_core_props_mapped(self):
        snap, _ = _run()
        f = snap.fields
        assert f["fp_model"] == "Pixel 7"
        assert f["fp_brand"] == "google"
        assert f["fp_manufacturer"] == "Google"
        assert f["fp_product_name"] == "panther"
        assert f["fp_device"] == "panther"
        assert f["fp_build_id"] == "UP1A.231005.007"
        assert f["fp_android_version"] == "14"
        assert f["fp_sdk"] == "34"
        assert f["fp_serialno"] == "1A2B3C4D"
        assert f["fp_imei"] == "351234567890123"
        assert f["fp_imsi"] == "250991234567890"
        assert f["fp_carrier"] == "Beeline"
        assert f["fp_carrier_numeric"] == "25099"
        assert f["fp_locale"] == "en-US"
        assert f["fp_timezone"] == "Europe/Moscow"

    def test_build_fingerprint_with_special_chars(self):
        snap, _ = _run()
        assert snap.fields["fp_build_fingerprint"].startswith("google/panther")

    def test_settings_and_files(self):
        snap, _ = _run()
        f = snap.fields
        assert f["fp_android_id"] == "abcd1234ef567890"
        assert f["fp_gaid"] == "11111111-2222-3333-4444-555555555555"
        assert f["fp_boot_id"] == "deadbeef-0000-1111-2222-333344445555"
        assert f["fp_wifi_mac"] == "aa:bb:cc:dd:ee:ff"
        assert f["fp_ip"] == "100.64.0.5"

    def test_screen_and_density(self):
        snap, _ = _run()
        f = snap.fields
        assert f["fp_screen_w"] == "1080"
        assert f["fp_screen_h"] == "2400"
        assert f["fp_density"] == "420"

    def test_wm_size_prefers_override(self):
        responses = _default_responses()
        responses["wm size"] = "Physical size: 1080x2400\nOverride size: 720x1600\n"
        snap, _ = _run(shell=_FakeShell(responses))
        assert snap.fields["fp_screen_w"] == "720"
        assert snap.fields["fp_screen_h"] == "1600"


class TestRawGetprop:
    def test_raw_getprop_captured(self):
        snap, _ = _run()
        assert "[ro.product.model]: [Pixel 7]" in snap.raw_getprop

    def test_raw_getprop_written_to_path(self, tmp_path):
        out = tmp_path / "art" / "getprop.txt"
        snap, _ = _run(raw_getprop_path=out)
        assert out.exists()
        assert "[ro.build.fingerprint]" in out.read_text(encoding="utf-8")
        assert snap.raw_getprop_path == str(out)

    def test_no_path_leaves_raw_path_none(self):
        snap, _ = _run()
        assert snap.raw_getprop_path is None


class TestResilience:
    def test_missing_fields_blank_not_crash(self):
        # Only getprop responds; everything else returns "".
        shell = _FakeShell({"getprop": _GETPROP_DUMP})
        snap, _ = _run(shell=shell)
        assert snap.fields["fp_model"] == "Pixel 7"
        assert snap.fields["fp_android_id"] == ""
        assert snap.fields["fp_wifi_mac"] == ""
        assert snap.fields["fp_screen_w"] == ""

    def test_failing_calls_are_swallowed(self):
        shell = _FakeShell(
            _default_responses(),
            fail={"wlan0/address", "advertising_id"},
        )
        snap, _ = _run(shell=shell)
        # The failing captures are blank, the rest survive.
        assert snap.fields["fp_wifi_mac"] == ""
        assert snap.fields["fp_gaid"] == ""
        assert snap.fields["fp_model"] == "Pixel 7"

    def test_settings_null_becomes_blank(self):
        responses = _default_responses()
        responses["android_id"] = "null\n"
        snap, _ = _run(shell=_FakeShell(responses))
        assert snap.fields["fp_android_id"] == ""


class TestMappingContract:
    def test_all_fields_are_csv_columns(self):
        snap, _ = _run()
        assert set(snap.fields).issubset(set(CSV_COLUMNS))

    def test_only_fp_columns_emitted(self):
        snap, _ = _run()
        assert all(k.startswith("fp_") for k in snap.fields)
