"""Device fingerprint snapshot over ADB.

Reads the full set of identity-bearing device properties at registration time
and maps them to the ``fp_*`` CSV columns. We never SET these fields — only READ
them — and we also keep the raw ``getprop`` dump as an artifact so nothing is
lost if GenFarmer's ``ChangeDevice`` touches an unlisted property.

The ADB ``shell`` callable is injectable (default: ``src.worker.tools._adb.shell``)
so unit tests run without a device. Individual captures are best-effort: a
failing or missing source (no SIM, no wlan0, locked settings) yields a blank
cell rather than aborting the whole snapshot.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.worker.tools._adb import shell as _adb_shell

Shell = Callable[..., Awaitable[str]]

# getprop key -> fp_* column.
_PROP_MAP: dict[str, str] = {
    "ro.product.model": "fp_model",
    "ro.product.brand": "fp_brand",
    "ro.product.manufacturer": "fp_manufacturer",
    "ro.product.name": "fp_product_name",
    "ro.product.device": "fp_device",
    "ro.build.fingerprint": "fp_build_fingerprint",
    "ro.build.id": "fp_build_id",
    "ro.build.version.release": "fp_android_version",
    "ro.build.version.sdk": "fp_sdk",
    "ro.serialno": "fp_serialno",
    "ro.ril.imei": "fp_imei",
    "gsm.sim.imsi": "fp_imsi",
    "gsm.sim.operator.alpha": "fp_carrier",
    "gsm.sim.operator.numeric": "fp_carrier_numeric",
    "persist.sys.locale": "fp_locale",
    "persist.sys.timezone": "fp_timezone",
}

# All fp_* columns this module is responsible for (used to pre-seed blanks).
_FP_COLUMNS: tuple[str, ...] = tuple(_PROP_MAP.values()) + (
    "fp_android_id",
    "fp_gaid",
    "fp_boot_id",
    "fp_wifi_mac",
    "fp_ip",
    "fp_screen_w",
    "fp_screen_h",
    "fp_density",
)

_GETPROP_LINE = re.compile(r"^\[(?P<key>[^\]]+)\]:\s*\[(?P<val>.*)\]$")


@dataclass
class FingerprintSnapshot:
    """A captured device fingerprint plus the raw getprop artifact."""

    serial: str
    fields: dict[str, str] = field(default_factory=dict)
    raw_getprop: str = ""
    raw_getprop_path: str | None = None


async def snapshot_fingerprint(
    serial: str,
    *,
    shell: Shell | None = None,
    raw_getprop_path: str | Path | None = None,
) -> FingerprintSnapshot:
    """Capture the full device fingerprint for ``serial``.

    If ``raw_getprop_path`` is given, the raw ``getprop`` dump is written there
    and the resolved path is recorded on the snapshot.
    """
    sh: Shell = shell or _adb_shell

    fields: dict[str, str] = {col: "" for col in _FP_COLUMNS}

    raw_getprop = await _safe(sh, serial, "getprop")
    fields.update(_parse_getprop(raw_getprop))

    fields["fp_android_id"] = _clean(
        await _safe(sh, serial, "settings get secure android_id")
    )
    fields["fp_gaid"] = _clean(
        await _safe(sh, serial, "settings get secure advertising_id")
    )
    fields["fp_boot_id"] = _clean(
        await _safe(sh, serial, "cat /proc/sys/kernel/random/boot_id")
    )
    fields["fp_wifi_mac"] = _clean(
        await _safe(sh, serial, "cat /sys/class/net/wlan0/address")
    )
    fields["fp_ip"] = _clean(
        await _safe(sh, serial, "ip route get 1.1.1.1")
    )
    if fields["fp_ip"]:
        fields["fp_ip"] = _parse_ip(fields["fp_ip"])

    w, h = _parse_wm_size(await _safe(sh, serial, "wm size"))
    fields["fp_screen_w"] = w
    fields["fp_screen_h"] = h
    fields["fp_density"] = _parse_wm_density(await _safe(sh, serial, "wm density"))

    snap = FingerprintSnapshot(serial=serial, fields=fields, raw_getprop=raw_getprop)

    if raw_getprop_path is not None and raw_getprop:
        out = Path(raw_getprop_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(raw_getprop, encoding="utf-8")
        snap.raw_getprop_path = str(out)

    return snap


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_getprop(dump: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (dump or "").splitlines():
        m = _GETPROP_LINE.match(line.strip())
        if not m:
            continue
        col = _PROP_MAP.get(m.group("key"))
        if col:
            out[col] = m.group("val").strip()
    return out


def _parse_wm_size(text: str) -> tuple[str, str]:
    """Return (width, height); prefer an Override size over Physical."""
    physical = override = None
    for line in (text or "").splitlines():
        m = re.search(r"(\d+)x(\d+)", line)
        if not m:
            continue
        if "Override" in line:
            override = (m.group(1), m.group(2))
        elif "Physical" in line:
            physical = (m.group(1), m.group(2))
        elif physical is None:
            physical = (m.group(1), m.group(2))
    chosen = override or physical
    return chosen if chosen else ("", "")


def _parse_wm_density(text: str) -> str:
    physical = override = ""
    for line in (text or "").splitlines():
        m = re.search(r"(\d+)", line)
        if not m:
            continue
        if "Override" in line:
            override = m.group(1)
        elif "Physical" in line:
            physical = m.group(1)
        elif not physical:
            physical = m.group(1)
    return override or physical


def _parse_ip(text: str) -> str:
    """Extract the source IP from ``ip route get`` output (or a bare IP)."""
    m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
    return m.group(1) if m else ""


def _clean(value: str) -> str:
    """Trim ADB output; treat ``null`` / empty as blank."""
    v = (value or "").strip()
    if not v or v.lower() == "null":
        return ""
    return v


async def _safe(shell: Shell, serial: str, cmd: str) -> str:
    """Run a shell command, returning ``""`` on any failure."""
    try:
        return await shell(serial, cmd)
    except Exception:
        return ""


__all__ = ["FingerprintSnapshot", "snapshot_fingerprint"]
