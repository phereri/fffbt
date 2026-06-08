"""Tests for the device identity rotator (``rotator.py``).

NoopRotator is the local/fleet capture-only path used now: it must NOT mutate
the device and must report reachable based on an injected probe. The abstract
interface contract is also pinned here.
"""

from __future__ import annotations

import asyncio

import pytest

from src.registration.rotator import (
    DeviceIdentityRotator,
    NoopRotator,
    RotationResult,
)


def _run(coro):
    return asyncio.run(coro)


class TestRotationResult:
    def test_fields(self):
        r = RotationResult(serial="dev1", rotated=False, reachable=True)
        assert r.serial == "dev1"
        assert r.rotated is False
        assert r.reachable is True
        assert r.detail == ""

    def test_ok_property(self):
        assert RotationResult("d", rotated=False, reachable=True).ok is True
        assert RotationResult("d", rotated=True, reachable=False).ok is False


class TestNoopRotator:
    def test_is_rotator(self):
        assert isinstance(NoopRotator(), DeviceIdentityRotator)

    def test_does_not_rotate(self):
        r = _run(NoopRotator().rotate("dev1"))
        assert r.rotated is False
        assert r.serial == "dev1"

    def test_reachable_true_by_default(self):
        # Default probe assumes reachable (capture-only, no verification).
        r = _run(NoopRotator().rotate("dev1"))
        assert r.reachable is True
        assert r.ok is True

    def test_uses_injected_probe(self):
        calls = []

        async def probe(serial: str) -> bool:
            calls.append(serial)
            return False

        r = _run(NoopRotator(reachable_probe=probe).rotate("dev1"))
        assert calls == ["dev1"]
        assert r.reachable is False
        assert r.ok is False

    def test_probe_exception_means_unreachable(self):
        async def probe(serial: str) -> bool:
            raise RuntimeError("offline")

        r = _run(NoopRotator(reachable_probe=probe).rotate("dev1"))
        assert r.reachable is False
        assert r.ok is False

    def test_detail_mentions_noop(self):
        r = _run(NoopRotator().rotate("dev1"))
        assert "noop" in r.detail.lower() or "capture" in r.detail.lower()


class TestInterface:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            DeviceIdentityRotator()  # type: ignore[abstract]
