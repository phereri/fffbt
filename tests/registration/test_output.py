"""Tests for the registration CSV writer (``output.py``).

No real device, no network — these exercise pure file IO in a tmp_path. The
writer must be append-only, header-on-create, schema-stable, and resilient to
partial/unknown row dicts.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from src.registration.output import (
    CSV_COLUMNS,
    append_account_row,
    row_from_parts,
)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_columns_match_spec(self):
        # The exact ordered schema from the design spec / .hermes.md.
        assert CSV_COLUMNS == [
            "username",
            "password",
            "full_name",
            "birthday",
            "phone_number",
            "phone_country",
            "fivesim_order_id",
            "registered_at",
            "device_adb_serial",
            "device_genfarmer_id",
            "device_connection_type",
            "fp_model",
            "fp_brand",
            "fp_manufacturer",
            "fp_product_name",
            "fp_device",
            "fp_build_fingerprint",
            "fp_build_id",
            "fp_android_version",
            "fp_sdk",
            "fp_serialno",
            "fp_android_id",
            "fp_gaid",
            "fp_imei",
            "fp_imsi",
            "fp_boot_id",
            "fp_wifi_mac",
            "fp_ip",
            "fp_screen_w",
            "fp_screen_h",
            "fp_density",
            "fp_locale",
            "fp_timezone",
            "fp_carrier",
            "fp_carrier_numeric",
            "raw_getprop_path",
            "trajectory_path",
            "status",
        ]

    def test_no_duplicate_columns(self):
        assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS))


# ---------------------------------------------------------------------------
# append_account_row
# ---------------------------------------------------------------------------


class TestAppendAccountRow:
    def test_creates_file_with_header(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(path, {"username": "alice", "status": "success"})
        assert path.exists()
        with open(path, newline="", encoding="utf-8") as fh:
            header = fh.readline().strip()
        assert header == ",".join(CSV_COLUMNS)
        rows = _read_rows(path)
        assert len(rows) == 1
        assert rows[0]["username"] == "alice"
        assert rows[0]["status"] == "success"

    def test_header_written_once_on_append(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(path, {"username": "alice"})
        append_account_row(path, {"username": "bob"})
        rows = _read_rows(path)
        assert [r["username"] for r in rows] == ["alice", "bob"]
        # Only one header line.
        text = path.read_text(encoding="utf-8")
        assert text.count("username,password") == 1

    def test_missing_fields_blank(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(path, {"username": "alice"})
        rows = _read_rows(path)
        assert rows[0]["password"] == ""
        assert rows[0]["fp_imei"] == ""

    def test_unknown_keys_ignored(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(
            path, {"username": "alice", "totally_unknown": "x", "fp_bogus": 1}
        )
        rows = _read_rows(path)
        assert set(rows[0].keys()) == set(CSV_COLUMNS)
        assert "totally_unknown" not in rows[0]

    def test_none_values_become_blank(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(path, {"username": "alice", "password": None})
        rows = _read_rows(path)
        assert rows[0]["password"] == ""

    def test_non_str_values_stringified(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(
            path, {"username": "alice", "fp_sdk": 34, "fp_screen_w": 1080}
        )
        rows = _read_rows(path)
        assert rows[0]["fp_sdk"] == "34"
        assert rows[0]["fp_screen_w"] == "1080"

    def test_commas_and_newlines_quoted(self, tmp_path):
        path = tmp_path / "accounts.csv"
        weird = "samsung/a,b\nc"
        append_account_row(path, {"fp_build_fingerprint": weird})
        rows = _read_rows(path)
        assert rows[0]["fp_build_fingerprint"] == weird

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "accounts.csv"
        append_account_row(path, {"username": "alice"})
        assert path.exists()

    def test_accepts_str_path(self, tmp_path):
        path = tmp_path / "accounts.csv"
        append_account_row(str(path), {"username": "alice"})
        assert path.exists()

    def test_returns_written_row(self, tmp_path):
        path = tmp_path / "accounts.csv"
        row = append_account_row(path, {"username": "alice"})
        assert isinstance(row, dict)
        assert row["username"] == "alice"
        assert set(row.keys()) == set(CSV_COLUMNS)


# ---------------------------------------------------------------------------
# row_from_parts
# ---------------------------------------------------------------------------


class TestRowFromParts:
    def test_flattens_result_fingerprint_and_meta(self):
        result = {
            "username": "alice",
            "password": "pw",
            "full_name": "Alice Smith",
            "birthday": "2000-01-01",
            "phone_number": "+79001234567",
            "phone_country": "russia",
            "fivesim_order_id": "11631253",
        }
        fingerprint = {
            "fp_model": "Pixel 7",
            "fp_brand": "google",
            "fp_imei": "123456789012345",
            "fp_sdk": "34",
        }
        row = row_from_parts(
            result=result,
            fingerprint=fingerprint,
            device_adb_serial="100.64.0.5:5555",
            device_connection_type="tailscale",
            device_genfarmer_id="gf-42",
            registered_at="2026-06-08T12:00:00Z",
            raw_getprop_path="/art/getprop.txt",
            trajectory_path="/art/traj",
            status="success",
        )
        assert row["username"] == "alice"
        assert row["fp_model"] == "Pixel 7"
        assert row["fp_imei"] == "123456789012345"
        assert row["device_adb_serial"] == "100.64.0.5:5555"
        assert row["device_connection_type"] == "tailscale"
        assert row["device_genfarmer_id"] == "gf-42"
        assert row["registered_at"] == "2026-06-08T12:00:00Z"
        assert row["raw_getprop_path"] == "/art/getprop.txt"
        assert row["trajectory_path"] == "/art/traj"
        assert row["status"] == "success"

    def test_result_object_with_as_dict(self):
        class _R:
            def as_dict(self):
                return {"username": "bob", "password": "x"}

        row = row_from_parts(result=_R(), fingerprint={})
        assert row["username"] == "bob"
        assert row["password"] == "x"

    def test_only_known_columns_survive(self):
        row = row_from_parts(
            result={"username": "a", "failure_reason": "boom", "notes": "n"},
            fingerprint={"fp_unknown": "z"},
        )
        assert set(row.keys()) <= set(CSV_COLUMNS)
        # failure_reason / notes are not CSV columns — dropped.
        assert "failure_reason" not in row
        assert "fp_unknown" not in row

    def test_defaults_blank_when_omitted(self):
        row = row_from_parts(result={}, fingerprint={})
        assert row["username"] == ""
        assert row["status"] == ""
        assert row["device_adb_serial"] == ""

    def test_roundtrips_through_append(self, tmp_path):
        path = tmp_path / "accounts.csv"
        row = row_from_parts(
            result={"username": "alice"},
            fingerprint={"fp_model": "Pixel 7"},
            device_adb_serial="dev1",
            status="success",
        )
        append_account_row(path, row)
        rows = _read_rows(path)
        assert rows[0]["username"] == "alice"
        assert rows[0]["fp_model"] == "Pixel 7"
        assert rows[0]["device_adb_serial"] == "dev1"
