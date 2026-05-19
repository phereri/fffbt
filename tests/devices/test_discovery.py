"""Unit tests for the pure reconciliation logic in discover_physical_devices.py.

Run from the repo root:

    python -m unittest tests.devices.test_discovery
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone


def _load_module():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "discover_physical_devices",
        repo_root / "scripts" / "discover_physical_devices.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


discover = _load_module()


NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
STALE_SECONDS = 120
STALE_THRESHOLD = NOW - timedelta(seconds=STALE_SECONDS)


def row(**kwargs) -> "discover.DeviceRow":
    defaults = dict(
        id="dev-1", alias="sm-n950f", adb_serial=None,
        tailscale_ipv4="100.0.0.1", status="offline", last_seen_at=None,
    )
    defaults.update(kwargs)
    return discover.DeviceRow(**defaults)


def live(**kwargs) -> "discover.LiveDevice":
    defaults = dict(
        serial="abcd1234", state="device", ip=None,
        seen_at=NOW, source="adb",
    )
    defaults.update(kwargs)
    return discover.LiveDevice(**defaults)


class ParseAdbDevicesTests(unittest.TestCase):
    def test_skips_header_and_blanks(self):
        out = discover.parse_adb_devices_l(
            "List of devices attached\n\n", NOW
        )
        self.assertEqual(out, [])

    def test_tcp_serial_captures_ip(self):
        out = discover.parse_adb_devices_l(
            "List of devices attached\n"
            "100.68.78.96:5555      device product:o1q model:SM_N950F\n",
            NOW,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].serial, "100.68.78.96:5555")
        self.assertEqual(out[0].ip, "100.68.78.96")
        self.assertEqual(out[0].state, "device")

    def test_usb_serial_has_no_ip(self):
        out = discover.parse_adb_devices_l(
            "List of devices attached\n"
            "abcd1234               offline\n",
            NOW,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].serial, "abcd1234")
        self.assertIsNone(out[0].ip)
        self.assertEqual(out[0].state, "offline")


class IndexLiveTests(unittest.TestCase):
    def test_most_recent_wins_for_same_serial(self):
        older = live(serial="s1", seen_at=NOW - timedelta(seconds=60))
        newer = live(serial="s1", state="offline", seen_at=NOW)
        by_serial, _ = discover.index_live([older, newer])
        self.assertEqual(by_serial["s1"].state, "offline")

    def test_ip_index_picks_most_recent(self):
        a = live(serial="x", ip="1.2.3.4", seen_at=NOW - timedelta(seconds=30))
        b = live(serial="y", ip="1.2.3.4", seen_at=NOW)
        _, by_ip = discover.index_live([a, b])
        self.assertEqual(by_ip["1.2.3.4"].serial, "y")


class BuildPlanTests(unittest.TestCase):
    def _build(self, rows, lives, reassign_serial=False):
        by_serial, by_ip = discover.index_live(lives)
        return discover.build_plan(
            rows, by_serial, by_ip, STALE_THRESHOLD, reassign_serial
        )

    def test_offline_to_online_when_ip_matches_fresh(self):
        r = row(tailscale_ipv4="100.0.0.1", status="offline")
        l = live(serial="100.0.0.1:5555", ip="100.0.0.1", seen_at=NOW)
        plans = self._build([r], [l])
        self.assertEqual(plans[0].new_status, "online")
        self.assertEqual(plans[0].event, "connected")
        self.assertEqual(plans[0].new_last_seen_at, NOW)

    def test_online_to_offline_when_no_match_within_window(self):
        r = row(status="online", last_seen_at=NOW - timedelta(hours=1))
        plans = self._build([r], [])
        self.assertEqual(plans[0].new_status, "offline")
        self.assertEqual(plans[0].event, "disconnected")

    def test_busy_row_is_left_alone(self):
        r = row(status="busy")
        plans = self._build([r], [])
        self.assertEqual(plans[0].new_status, "busy")
        self.assertIsNone(plans[0].event)
        self.assertIsNone(plans[0].new_adb_serial)

    def test_maintenance_row_is_left_alone(self):
        r = row(status="maintenance")
        l = live(serial="100.0.0.1:5555", ip="100.0.0.1", seen_at=NOW)
        plans = self._build([r], [l])
        self.assertEqual(plans[0].new_status, "maintenance")
        self.assertIsNone(plans[0].event)

    def test_adb_serial_backfilled_when_only_ip_known(self):
        # First time we see the device — matched by IP, but ADB / heartbeat
        # reports a USB-form serial. We capture it for next time.
        r = row(adb_serial=None, tailscale_ipv4="100.0.0.1", status="offline")
        l = live(serial="HW123456", ip="100.0.0.1", seen_at=NOW, source="heartbeat")
        plans = self._build([r], [l])
        self.assertEqual(plans[0].new_adb_serial, "HW123456")
        self.assertEqual(plans[0].new_status, "online")

    def test_adb_serial_not_overwritten_without_reassign_flag(self):
        r = row(adb_serial="OLD", tailscale_ipv4="100.0.0.1", status="online",
                last_seen_at=NOW)
        l = live(serial="NEW", ip="100.0.0.1", seen_at=NOW, source="heartbeat")
        plans = self._build([r], [l], reassign_serial=False)
        self.assertIsNone(plans[0].new_adb_serial)

    def test_adb_serial_overwritten_with_reassign_flag(self):
        r = row(adb_serial="OLD", tailscale_ipv4="100.0.0.1", status="online",
                last_seen_at=NOW)
        # Match by IP only (OLD doesn't appear in live data), discovery sees NEW.
        l = live(serial="NEW", ip="100.0.0.1", seen_at=NOW, source="heartbeat")
        plans = self._build([r], [l], reassign_serial=True)
        self.assertEqual(plans[0].new_adb_serial, "NEW")

    def test_offline_state_in_adb_does_not_count_as_online(self):
        # `adb devices -l` may list a device as 'offline' / 'unauthorized'.
        # We only flip to online when the reported state is 'device'.
        r = row(status="offline", tailscale_ipv4="100.0.0.1")
        l = live(serial="100.0.0.1:5555", state="offline", ip="100.0.0.1", seen_at=NOW)
        plans = self._build([r], [l])
        self.assertEqual(plans[0].new_status, "offline")
        self.assertIsNone(plans[0].event)

    def test_stale_heartbeat_does_not_flip_to_online(self):
        old = NOW - timedelta(seconds=STALE_SECONDS + 30)
        r = row(status="offline", tailscale_ipv4="100.0.0.1")
        l = live(serial="100.0.0.1:5555", ip="100.0.0.1", seen_at=old,
                 source="heartbeat")
        plans = self._build([r], [l])
        self.assertEqual(plans[0].new_status, "offline")
        self.assertIsNone(plans[0].event)

    def test_match_by_adb_serial_preferred_over_ip(self):
        r = row(adb_serial="HW999", tailscale_ipv4="100.0.0.1", status="offline")
        l_ip = live(serial="SOMEONE_ELSE", ip="100.0.0.1", seen_at=NOW)
        l_serial = live(serial="HW999", seen_at=NOW)
        plans = self._build([r], [l_ip, l_serial])
        self.assertEqual(plans[0].matched.serial, "HW999")
        self.assertEqual(plans[0].new_status, "online")


class ManagementApiHelperTests(unittest.TestCase):
    def test_parse_timestamptz_handles_z_suffix(self):
        ts = discover._parse_timestamptz("2026-05-19T12:00:00Z")
        self.assertEqual(ts, NOW)

    def test_parse_timestamptz_handles_offset(self):
        ts = discover._parse_timestamptz("2026-05-19T12:00:00+00:00")
        self.assertEqual(ts, NOW)

    def test_parse_timestamptz_passthrough_none(self):
        self.assertIsNone(discover._parse_timestamptz(None))

    def test_json_default_serializes_datetime(self):
        self.assertEqual(
            discover._json_default(NOW), "2026-05-19T12:00:00+00:00"
        )


if __name__ == "__main__":
    unittest.main()
