"""Unit tests for scripts/reconnect_devices.py pure logic.

Run from the repo root:

    python -m unittest tests.devices.test_reconnect
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


def _load_module():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "reconnect_devices",
        repo_root / "scripts" / "reconnect_devices.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reconnect = _load_module()


def row(**kwargs) -> "reconnect.DeviceRow":
    defaults = dict(
        id="00000000-0000-0000-0000-000000000001",
        alias="dev-a",
        device_id="DEV_ID_A",
        adb_serial=None,
        tailscale_ipv4="100.0.0.1",
        adb_connect_target="100.0.0.1:5555",
        status="offline",
    )
    defaults.update(kwargs)
    return reconnect.DeviceRow(**defaults)


class ClassifyAdbOutputTests(unittest.TestCase):
    def test_connected_first_time(self):
        self.assertEqual(
            reconnect.classify_adb_output("connected to 100.0.0.1:5555\n", "", 0),
            "connected",
        )

    def test_already_connected(self):
        self.assertEqual(
            reconnect.classify_adb_output("already connected to 100.0.0.1:5555\n", "", 0),
            "already",
        )

    def test_failed_to_connect(self):
        self.assertEqual(
            reconnect.classify_adb_output(
                "failed to connect to '100.0.0.1:5555': Connection refused\n", "", 0,
            ),
            "failed",
        )

    def test_unable_to_connect(self):
        self.assertEqual(
            reconnect.classify_adb_output("unable to connect to 100.0.0.1:5555\n", "", 1),
            "failed",
        )

    def test_no_route_to_host_on_stderr(self):
        self.assertEqual(
            reconnect.classify_adb_output(
                "", "error: no route to host: 100.0.0.1\n", 1,
            ),
            "failed",
        )

    def test_timeout_synthetic_message(self):
        # adb_connect() synthesises this string on subprocess timeout.
        self.assertEqual(
            reconnect.classify_adb_output("", "timed out after 15s", 124),
            "failed",
        )

    def test_unknown_output_is_failure(self):
        # Conservative default: any text we can't positively recognise as
        # success counts as a failure so we don't promote a row to 'online'
        # we aren't sure about.
        self.assertEqual(
            reconnect.classify_adb_output("???\n", "", 0),
            "failed",
        )


class FilterReconnectCandidatesTests(unittest.TestCase):
    def test_offline_with_target_is_candidate(self):
        r = row(status="offline", adb_connect_target="100.0.0.1:5555")
        cands, skipped = reconnect.filter_reconnect_candidates([r])
        self.assertEqual([c.id for c in cands], [r.id])
        self.assertEqual(skipped, [])

    def test_offline_without_target_is_skipped(self):
        r = row(status="offline", adb_connect_target=None, tailscale_ipv4=None)
        cands, skipped = reconnect.filter_reconnect_candidates([r])
        self.assertEqual(cands, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].outcome, "skipped")
        self.assertIn("no adb_connect_target", skipped[0].message)

    def test_online_is_skipped(self):
        r = row(status="online")
        cands, skipped = reconnect.filter_reconnect_candidates([r])
        self.assertEqual(cands, [])
        self.assertEqual(skipped[0].outcome, "skipped")
        self.assertIn("already online", skipped[0].message)

    def test_busy_is_skipped_and_protected(self):
        r = row(status="busy")
        cands, skipped = reconnect.filter_reconnect_candidates([r])
        self.assertEqual(cands, [])
        self.assertEqual(skipped[0].outcome, "skipped")
        self.assertIn("status=busy", skipped[0].message)

    def test_maintenance_is_skipped_and_protected(self):
        r = row(status="maintenance")
        cands, skipped = reconnect.filter_reconnect_candidates([r])
        self.assertEqual(cands, [])
        self.assertEqual(skipped[0].outcome, "skipped")
        self.assertIn("status=maintenance", skipped[0].message)


class SelectSingleDeviceTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row(
                id="00000000-0000-0000-0000-000000000001",
                alias="dev-a", device_id="DEV_ID_A",
                tailscale_ipv4="100.0.0.1",
                adb_connect_target="100.0.0.1:5555",
            ),
            row(
                id="00000000-0000-0000-0000-000000000002",
                alias="dev-b", device_id="DEV_ID_B",
                tailscale_ipv4="100.0.0.2",
                adb_connect_target="100.0.0.2:5555",
            ),
        ]

    def test_match_by_alias(self):
        r = reconnect.select_single_device(self.rows, "dev-b")
        self.assertEqual(r.id, "00000000-0000-0000-0000-000000000002")

    def test_match_by_device_id(self):
        r = reconnect.select_single_device(self.rows, "DEV_ID_A")
        self.assertEqual(r.alias, "dev-a")

    def test_match_by_ipv4(self):
        r = reconnect.select_single_device(self.rows, "100.0.0.2")
        self.assertEqual(r.alias, "dev-b")

    def test_match_by_uuid(self):
        r = reconnect.select_single_device(
            self.rows, "00000000-0000-0000-0000-000000000001"
        )
        self.assertEqual(r.alias, "dev-a")

    def test_no_match_raises(self):
        with self.assertRaises(LookupError):
            reconnect.select_single_device(self.rows, "nonexistent")

    def test_empty_selector_raises(self):
        with self.assertRaises(ValueError):
            reconnect.select_single_device(self.rows, "   ")

    def test_ambiguous_match_raises(self):
        # Two rows where one's alias collides with another's device_id.
        rows = [
            row(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                alias="shared", device_id="DEV_X"),
            row(id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                alias="dev-y", device_id="shared"),
        ]
        with self.assertRaises(LookupError) as cm:
            reconnect.select_single_device(rows, "shared")
        self.assertIn("multiple rows", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
