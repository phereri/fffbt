"""Device identity rotation behind an interface.

Per the design (.hermes.md §"Device identity"): rotate-then-capture. Locally /
on the current fleet we run **capture-only** via ``NoopRotator`` — it does not
mutate the device, it only (optionally) verifies the device is reachable before
the fingerprint snapshot + registration proceed.

``GenFarmerAutoRotator`` (fleet) drives GenFarmer ChangeDevice via
``src.genfarmer.changedevice``: apply a fresh, guaranteed Android-12+ identity,
then verify the phone returns on ADB after the change-reboot. Validated on the
real farm 2026-06-11 (see ``docs/runbooks/changedevice.md``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

from src.genfarmer.changedevice import ChangeDeviceClient, default_client

ReachableProbe = Callable[[str], Awaitable[bool]]


@dataclass
class RotationResult:
    """Outcome of a ``rotate`` call."""

    serial: str
    rotated: bool
    reachable: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the device is reachable (rotation may legitimately be a no-op)."""
        return self.reachable


class DeviceIdentityRotator(ABC):
    """Rotate (or not) a device's identity, then report reachability."""

    @abstractmethod
    async def rotate(self, serial: str) -> RotationResult:
        """Rotate identity for ``serial`` and verify the device is reachable."""
        raise NotImplementedError


async def _assume_reachable(serial: str) -> bool:
    return True


class NoopRotator(DeviceIdentityRotator):
    """Capture-only rotator: never mutates the device.

    Used for local emulators and the current fleet phase where GenFarmer
    ChangeDevice automation is not wired. An optional ``reachable_probe`` (e.g.
    a small ADB ping) decides reachability; the default assumes reachable.
    """

    def __init__(self, *, reachable_probe: ReachableProbe | None = None) -> None:
        self._probe: ReachableProbe = reachable_probe or _assume_reachable

    async def rotate(self, serial: str) -> RotationResult:
        try:
            reachable = await self._probe(serial)
        except Exception as exc:  # probe failure => treat as unreachable
            return RotationResult(
                serial=serial,
                rotated=False,
                reachable=False,
                detail=f"noop (capture-only); reachability probe failed: {exc}",
            )
        return RotationResult(
            serial=serial,
            rotated=False,
            reachable=bool(reachable),
            detail="noop (capture-only); no identity change performed",
        )


class GenFarmerAutoRotator(DeviceIdentityRotator):
    """Fleet rotator: apply a fresh Android>=``min_android`` identity via ChangeDevice.

    ``rotate`` fetches a random profile (looping until Android >= ``min_android``),
    applies it with a freshly generated ``serialno`` (a NEW account), then waits
    for the phone to return on ADB after the change-reboot (~90 s).

    To RETURN to an existing account, do not use this rotator — call
    ``ChangeDeviceClient.apply(serial, saved_profile, keep_serial=True)`` to
    restore that account's exact saved device identity.

    ``clear_data=True`` additionally wipes app data and rotates the real
    ``android_id`` (use for maximal per-account isolation; it removes app state).
    """

    def __init__(
        self,
        client: ChangeDeviceClient | None = None,
        *,
        min_android: int = 12,
        clear_data: bool = False,
        reconnect_timeout: float = 300.0,
    ) -> None:
        self._client = client or default_client()
        self._min_android = min_android
        self._clear_data = clear_data
        self._reconnect_timeout = reconnect_timeout

    async def rotate(self, serial: str) -> RotationResult:
        try:
            if not await self._client.ready(serial):
                return RotationResult(
                    serial, rotated=False, reachable=False,
                    detail="device is not GenFarmer-ready (ROM helper missing)",
                )
            profile = await self._client.fetch_random(min_android=self._min_android)
            await self._client.apply(
                serial, profile, clear_data=self._clear_data,
                keep_serial=False, require_ready=False,
            )
            reachable = await self._client.wait_reconnect(serial, timeout=self._reconnect_timeout)
            return RotationResult(
                serial, rotated=True, reachable=reachable,
                detail=f"ChangeDevice -> {profile.summary()}",
            )
        except Exception as exc:  # never raise out of rotate()
            return RotationResult(
                serial, rotated=False, reachable=False,
                detail=f"ChangeDevice failed: {exc}",
            )


__all__ = [
    "DeviceIdentityRotator",
    "GenFarmerAutoRotator",
    "NoopRotator",
    "RotationResult",
    "ReachableProbe",
]
