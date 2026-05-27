"""Caption and hashtag selection MVP for Instagram Reels.

Provides safe static football/FIFA-themed captions, a fixed pool of hashtags,
and helpers to assemble the final caption string.
"""

from __future__ import annotations

import random

HASHTAG_POOL: list[str] = [
    "#football",
    "#fifa",
    "#worldcup",
    "#soccer",
    "#futbol",
    "#matchday",
    "#reels",
    "#footballreels",
    "#footballfans",
    "#sports",
    "#footballlife",
    "#footballmoments",
    "#beautifulgame",
    "#footballclips",
    "#soccerlife",
]

CAPTION_TEMPLATES: list[str] = [
    "Football fans are already looking ahead to the next big FIFA stage. The countdown energy is real.",
    "The road to the next major football tournament is heating up. Every match feels like part of the story.",
    "Big football moments start long before kickoff. Teams, fans, and the whole world are getting ready.",
    "The next major football stage is already on everyone's mind. The build-up has its own kind of magic.",
    "FIFA nights, football dreams, and the road to the next big tournament. The energy keeps building.",
]

DEFAULT_HASHTAG_COUNT = 5
MIN_HASHTAG_COUNT = 3
MAX_RANDOM_HASHTAG_COUNT = 7
MAX_HASHTAG_COUNT = 30


def select_caption_template(*, rng: random.Random | None = None) -> str:
    """Return a safe static football/FIFA caption template."""
    r = rng or random
    return r.choice(CAPTION_TEMPLATES)


def normalize_hashtag_count(count: int | None) -> int:
    """Clamp hashtag count for MVP validation captions to 3-7 tags."""
    if count is None:
        return DEFAULT_HASHTAG_COUNT
    return max(MIN_HASHTAG_COUNT, min(MAX_RANDOM_HASHTAG_COUNT, count))


def select_hashtags(
    count: int = DEFAULT_HASHTAG_COUNT,
    *,
    pool: list[str] | None = None,
    rng: random.Random | None = None,
) -> list[str]:
    """Return a random subset of hashtags from the pool.

    ``count`` is clamped to the pool size so it never raises.
    """
    source = pool if pool is not None else HASHTAG_POOL
    if not source:
        return []
    k = min(normalize_hashtag_count(count), len(source))
    r = rng or random
    return r.sample(source, k)


def build_validation_caption(
    base_caption: str | None = None,
    *,
    hashtag_count: int | None = None,
    rng: random.Random | None = None,
) -> tuple[str, list[str], str]:
    """Build a non-placeholder validation caption and selected hashtags.

    Returns ``(full_caption, hashtags, base_caption)``. If a caller provides a
    non-empty base caption it is used as-is; otherwise a safe football/FIFA
    template is selected.
    """
    base = (base_caption or "").strip() or select_caption_template(rng=rng)
    hashtags = select_hashtags(
        normalize_hashtag_count(hashtag_count),
        rng=rng,
    )
    return build_caption(base, hashtags), hashtags, base


def build_caption(caption: str, hashtags: list[str]) -> str:
    """Assemble final caption: ``caption + "\\n\\n" + hashtags``.

    If either part is empty the other is returned as-is (no stray newlines).
    """
    tag_line = " ".join(hashtags)
    if not caption:
        return tag_line
    if not tag_line:
        return caption
    return caption + "\n\n" + tag_line
