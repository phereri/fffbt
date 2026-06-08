"""Device identity rotation behind an interface.

Per the design (.hermes.md §"Device identity"): rotate-then-capture. Locally /
on the current fleet we run **capture-only** via ``NoopRotator`` — it does not
mutate the device, it only (optionally) verifies the device is reachable before
the fingerprint snapshot + registration proceed.

``GenFarmerAutoRotator`` (fleet: trigger ChangeDevice via the GenFarmer
Automation REST, THEN verify ADB reachable) is a TODO to be implemented and
verified on the real farm — it is deliberately not built yet.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

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


__all__ = [
    "DeviceIdentityRotator",
    "NoopRotator",
    "RotationResult",
    "ReachableProbe",
]
