"""Atomic CSV output for registered Instagram accounts.

One row per registration attempt. The writer is:
- **append-only** — never rewrites existing rows;
- **header-on-create** — writes the header exactly once, when the file is new;
- **schema-stable** — every row has exactly ``CSV_COLUMNS``, in order;
- **resilient** — partial/unknown row dicts are coerced (unknown keys dropped,
  missing keys blank, ``None`` → ``""``, non-strings stringified, commas /
  newlines quoted by the stdlib ``csv`` module).

``row_from_parts`` flattens a ``RegistrationResult`` (dict or object with
``as_dict``) + a fingerprint dict + per-run metadata into a single CSV row.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

# Exact ordered schema from the design spec / .hermes.md. Order is the CSV
# column order; do not reorder without updating the spec + tests.
CSV_COLUMNS: list[str] = [
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

# Metadata fields that live on the row but come from the orchestrator, not the
# agent result or the fingerprint snapshot.
_META_FIELDS = (
    "registered_at",
    "device_adb_serial",
    "device_genfarmer_id",
    "device_connection_type",
    "raw_getprop_path",
    "trajectory_path",
    "status",
)


def _cell(value: Any) -> str:
    """Coerce a value to a CSV cell string (``None`` → empty)."""
    if value is None:
        return ""
    return str(value)


def normalize_row(row: dict[str, Any]) -> dict[str, str]:
    """Return a row containing exactly ``CSV_COLUMNS``.

    Unknown keys are dropped, missing keys become ``""``, every value is
    stringified via :func:`_cell`.
    """
    return {col: _cell(row.get(col)) for col in CSV_COLUMNS}


def row_from_parts(
    *,
    result: Any = None,
    fingerprint: dict[str, Any] | None = None,
    device_adb_serial: str | None = None,
    device_genfarmer_id: str | None = None,
    device_connection_type: str | None = None,
    registered_at: str | None = None,
    raw_getprop_path: str | None = None,
    trajectory_path: str | None = None,
    status: str | None = None,
) -> dict[str, str]:
    """Flatten a result + fingerprint + metadata into a normalized CSV row.

    ``result`` may be a dict or any object exposing ``as_dict()`` (e.g.
    ``RegistrationResult``). Only keys that are also ``CSV_COLUMNS`` survive —
    result-only fields such as ``failure_reason`` / ``notes`` are intentionally
    dropped from the CSV (they live in trajectories / logs, not the account row).
    """
    merged: dict[str, Any] = {}

    result_dict = _as_dict(result)
    merged.update(result_dict)

    if fingerprint:
        merged.update(fingerprint)

    meta = {
        "device_adb_serial": device_adb_serial,
        "device_genfarmer_id": device_genfarmer_id,
        "device_connection_type": device_connection_type,
        "registered_at": registered_at,
        "raw_getprop_path": raw_getprop_path,
        "trajectory_path": trajectory_path,
        "status": status,
    }
    for key, value in meta.items():
        if value is not None:
            merged[key] = value

    return normalize_row(merged)


def append_account_row(
    path: str | Path, row: dict[str, Any]
) -> dict[str, str]:
    """Append one normalized row to the CSV at ``path``.

    Creates parent directories and writes the header if the file does not yet
    exist (or is empty). Returns the normalized row that was written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    normalized = normalize_row(row)
    write_header = not target.exists() or target.stat().st_size == 0

    with open(target, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(normalized)

    return normalized


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    as_dict = getattr(obj, "as_dict", None)
    if callable(as_dict):
        out = as_dict()
        if isinstance(out, dict):
            return out
    return {}


__all__ = [
    "CSV_COLUMNS",
    "append_account_row",
    "row_from_parts",
    "normalize_row",
]
