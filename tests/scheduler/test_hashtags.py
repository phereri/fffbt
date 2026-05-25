"""Tests for hashtag selection MVP (FFF-33)."""

from __future__ import annotations

import random
import subprocess
import sys

REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()
sys.path.insert(0, f"{REPO_ROOT}/src")

from scheduler.hashtags import (
    DEFAULT_HASHTAG_COUNT,
    HASHTAG_POOL,
    build_caption,
    select_hashtags,
)


class TestSelectHashtags:
    def test_returns_requested_count(self):
        tags = select_hashtags(5)
        assert len(tags) == 5

    def test_default_count(self):
        tags = select_hashtags()
        assert len(tags) == DEFAULT_HASHTAG_COUNT

    def test_clamps_to_pool_size(self):
        pool = ["#a", "#b", "#c"]
        tags = select_hashtags(10, pool=pool)
        assert len(tags) == 3
        assert set(tags) == set(pool)

    def test_empty_pool(self):
        assert select_hashtags(5, pool=[]) == []

    def test_all_from_pool(self):
        tags = select_hashtags(5)
        for tag in tags:
            assert tag in HASHTAG_POOL

    def test_no_duplicates(self):
        tags = select_hashtags(15)
        assert len(tags) == len(set(tags))

    def test_deterministic_with_rng(self):
        rng = random.Random(42)
        tags1 = select_hashtags(5, rng=rng)
        rng = random.Random(42)
        tags2 = select_hashtags(5, rng=rng)
        assert tags1 == tags2

    def test_zero_count(self):
        assert select_hashtags(0) == []

    def test_pool_has_hashtag_prefix(self):
        for tag in HASHTAG_POOL:
            assert tag.startswith("#"), f"{tag} missing # prefix"


class TestBuildCaption:
    def test_caption_with_hashtags(self):
        result = build_caption("Great goal", ["#football", "#goal"])
        assert result == "Great goal\n\n#football #goal"

    def test_empty_caption(self):
        result = build_caption("", ["#football", "#goal"])
        assert result == "#football #goal"

    def test_empty_hashtags(self):
        result = build_caption("Great goal", [])
        assert result == "Great goal"

    def test_both_empty(self):
        result = build_caption("", [])
        assert result == ""

    def test_multiline_caption(self):
        result = build_caption("Line 1\nLine 2", ["#tag"])
        assert result == "Line 1\nLine 2\n\n#tag"

    def test_single_hashtag(self):
        result = build_caption("Caption", ["#solo"])
        assert result == "Caption\n\n#solo"
