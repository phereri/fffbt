"""Tests for the fallback identity generator (``identity.py``).

The agent normally invents its own identity; this is a deterministic fallback.
Seeded RNG must make output reproducible. Birthday must be a valid 18+ ISO date.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from src.registration.identity import Identity, generate_identity


class TestGenerateIdentity:
    def test_returns_identity(self):
        ident = generate_identity(rng=random.Random(0))
        assert isinstance(ident, Identity)

    def test_seeded_is_deterministic(self):
        a = generate_identity(rng=random.Random(42))
        b = generate_identity(rng=random.Random(42))
        assert a == b

    def test_different_seeds_differ(self):
        a = generate_identity(rng=random.Random(1))
        b = generate_identity(rng=random.Random(2))
        assert a != b

    def test_full_name_two_parts(self):
        ident = generate_identity(rng=random.Random(7))
        parts = ident.full_name.split()
        assert len(parts) >= 2
        assert all(p.isalpha() for p in parts)

    def test_username_charset(self):
        ident = generate_identity(rng=random.Random(7))
        assert ident.username
        assert all(c.isalnum() or c in "._" for c in ident.username)
        assert ident.username[0].isalnum()

    def test_password_strength(self):
        ident = generate_identity(rng=random.Random(7))
        pw = ident.password
        assert len(pw) >= 10
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)

    def test_birthday_is_iso_date(self):
        ident = generate_identity(rng=random.Random(7))
        d = dt.date.fromisoformat(ident.birthday)
        assert isinstance(d, dt.date)

    def test_birthday_is_adult(self):
        today = dt.date(2026, 6, 8)
        ident = generate_identity(rng=random.Random(7), today=today)
        d = dt.date.fromisoformat(ident.birthday)
        age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        assert 18 <= age <= 60

    def test_min_age_respected(self):
        today = dt.date(2026, 6, 8)
        # Force min_age high; everyone must be at least that old.
        for seed in range(20):
            ident = generate_identity(
                rng=random.Random(seed), today=today, min_age=30, max_age=40
            )
            d = dt.date.fromisoformat(ident.birthday)
            age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
            assert 30 <= age <= 40

    def test_as_dict_keys(self):
        ident = generate_identity(rng=random.Random(7))
        d = ident.as_dict()
        assert set(d) == {"username", "password", "full_name", "birthday"}
