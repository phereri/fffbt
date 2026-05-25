"""Hashtag selection MVP for Instagram Reels.

Provides a fixed pool of hashtags and helpers to select a random subset
and assemble the final caption string.
"""

from __future__ import annotations

import random

HASHTAG_POOL: list[str] = [
    "#football",
    "#soccer",
    "#goal",
    "#goals",
    "#futbol",
    "#footballskills",
    "#soccerskills",
    "#matchday",
    "#highlights",
    "#footballhighlights",
    "#bestgoals",
    "#topgoals",
    "#reels",
    "#reelsfootball",
    "#instafootball",
    "#footballreels",
    "#soccerreels",
    "#footballfans",
    "#footballlife",
    "#soccerlife",
    "#footballmoments",
    "#beautifulgame",
    "#golaso",
    "#golazo",
    "#futbolhighlights",
    "#footballtiktok",
    "#footballclips",
    "#soccergoals",
    "#footballgoals",
    "#footballvideo",
]

DEFAULT_HASHTAG_COUNT = 10
MAX_HASHTAG_COUNT = 30


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
    k = min(count, len(source))
    r = rng or random
    return r.sample(source, k)


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
