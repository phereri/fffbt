"""Fallback identity generation for Instagram registration.

The agent is expected to invent its own username / password / full name /
birthday at run time. This module is a deterministic fallback used only when a
local identity is needed (tests, seeding, or if the agent declines to choose).

Seeded via an injectable ``random.Random`` so output is reproducible.
"""

from __future__ import annotations

import datetime as dt
import random
import string
from dataclasses import asdict, dataclass

_FIRST_NAMES = (
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery",
    "Quinn", "Parker", "Sam", "Drew", "Skyler", "Cameron", "Reese", "Devin",
    "Emerson", "Hayden", "Rowan", "Sawyer", "Blake", "Charlie", "Finley", "Kai",
)

_LAST_NAMES = (
    "Carter", "Reyes", "Bennett", "Foster", "Hughes", "Brooks", "Murphy",
    "Sullivan", "Coleman", "Hayes", "Russell", "Griffin", "Diaz", "Powell",
    "Long", "Patterson", "Flores", "Washington", "Butler", "Simmons", "Foster",
    "Bryant", "Alexander", "Russo",
)

_PW_SYMBOLS = "!@#$%_-"


@dataclass(frozen=True)
class Identity:
    """A generated account identity."""

    username: str
    password: str
    full_name: str
    birthday: str  # ISO YYYY-MM-DD

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def generate_identity(
    *,
    rng: random.Random | None = None,
    today: dt.date | None = None,
    min_age: int = 18,
    max_age: int = 45,
) -> Identity:
    """Generate a plausible 18+ identity. Deterministic when ``rng`` is seeded."""
    r = rng or random.Random()
    day = today or dt.date.today()

    first = r.choice(_FIRST_NAMES)
    last = r.choice(_LAST_NAMES)
    full_name = f"{first} {last}"

    username = _make_username(r, first, last)
    password = _make_password(r)
    birthday = _make_birthday(r, day, min_age, max_age)

    return Identity(
        username=username,
        password=password,
        full_name=full_name,
        birthday=birthday,
    )


def _make_username(r: random.Random, first: str, last: str) -> str:
    sep = r.choice(("", "_", ".", ""))
    num = r.randint(0, 9999)
    base = f"{first.lower()}{sep}{last.lower()}{num}"
    # Guarantee the first char is alphanumeric and charset is IG-safe.
    base = "".join(c for c in base if c.isalnum() or c in "._")
    if not base or not base[0].isalnum():
        base = f"u{base}"
    return base[:30]


def _make_password(r: random.Random) -> str:
    length = r.randint(12, 16)
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, _PW_SYMBOLS]
    # Guarantee at least one of each required class.
    chars = [r.choice(p) for p in pools]
    all_chars = string.ascii_letters + string.digits + _PW_SYMBOLS
    chars += [r.choice(all_chars) for _ in range(length - len(chars))]
    r.shuffle(chars)
    return "".join(chars)


def _make_birthday(r: random.Random, today: dt.date, min_age: int, max_age: int) -> str:
    age = r.randint(min_age, max_age)
    year = today.year - age
    month = r.randint(1, 12)
    # Keep day valid for any month and ensure the person has already had their
    # birthday this calendar year (so computed age >= min_age holds).
    day = r.randint(1, 28)
    bd = dt.date(year, month, day)
    if (bd.month, bd.day) > (today.month, today.day):
        # Birthday hasn't occurred yet this year -> shift one more year back so
        # the realized age stays within [min_age, max_age].
        bd = dt.date(year - 1, month, day)
    return bd.isoformat()


__all__ = ["Identity", "generate_identity"]
