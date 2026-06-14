"""Tests for the self-recovering SMS recipe ladder (``ladder.py``).

No device/network/agent. A fake runner factory returns scripted
``RegistrationResult``s so we can assert the loop's control flow: stop on
success, stop on a fatal reason, advance on a recoverable reason, respect
max-attempts, and exhaust the ladder.
"""

from __future__ import annotations

import asyncio

import pytest

from src.registration.ladder import (
    CONTINUE,
    DEFAULT_LADDER,
    STOP_FATAL,
    STOP_SUCCESS,
    Recipe,
    RecipeLadder,
    classify,
    load_recipes,
)
from src.registration.result import RegistrationResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self, result: RegistrationResult, log: list) -> None:
        self._result = result
        self._log = log

    async def run(self) -> RegistrationResult:
        self._log.append(self._result)
        return self._result


def _scripted_factory(results: list[RegistrationResult]):
    """A runner factory that returns ``results[index]`` per attempt."""
    ran: list[RegistrationResult] = []
    recipes_seen: list[Recipe] = []

    def factory(recipe: Recipe, index: int):
        recipes_seen.append(recipe)
        return _FakeRunner(results[index], ran)

    return factory, ran, recipes_seen


def _ok(**kw) -> RegistrationResult:
    return RegistrationResult(success=True, username="alice", **kw)


def _fail(reason: str) -> RegistrationResult:
    return RegistrationResult(success=False, failure_reason=reason)


def _run(ladder: RecipeLadder):
    return asyncio.run(ladder.run())


RECIPES = [
    Recipe("5sim", "austria", "virtual51,any", 0.6, label="a"),
    Recipe("5sim", "croatia", "virtual4,any", 0.6, label="b"),
    Recipe("smspool", "usa", label="c"),
]


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


def test_classify_success():
    assert classify(_ok()) == STOP_SUCCESS


def test_classify_recoverable_sms_failure():
    assert classify(_fail("phone_verification_failed")) == CONTINUE


def test_classify_fatal_rate_limit():
    assert classify(_fail("rate_limited")) == STOP_FATAL


def test_classify_fatal_device_unreachable():
    assert classify(_fail("device_unreachable")) == STOP_FATAL


def test_classify_agent_exception_is_recoverable():
    assert classify(_fail("agent_exception: boom")) == CONTINUE


def test_classify_unknown_retry_by_default():
    assert classify(_fail("something_weird")) == CONTINUE


def test_classify_unknown_fatal_when_retry_unknown_false():
    assert classify(_fail("something_weird"), retry_unknown=False) == STOP_FATAL


# ---------------------------------------------------------------------------
# RecipeLadder.run()
# ---------------------------------------------------------------------------


def test_first_recipe_succeeds_stops_immediately():
    factory, ran, seen = _scripted_factory([_ok(), _fail("x"), _fail("y")])
    out = _run(RecipeLadder(RECIPES, factory))
    assert out.success is True
    assert out.stopped_reason == "success"
    assert len(ran) == 1  # did not try recipe 2 or 3
    assert out.winning_recipe is RECIPES[0]


def test_advances_past_recoverable_then_succeeds():
    factory, ran, seen = _scripted_factory(
        [_fail("phone_verification_failed"), _ok(), _fail("z")]
    )
    out = _run(RecipeLadder(RECIPES, factory))
    assert out.success is True
    assert len(ran) == 2
    assert [r.country for r in seen] == ["austria", "croatia"]
    assert out.winning_recipe is RECIPES[1]


def test_fatal_reason_stops_early():
    factory, ran, seen = _scripted_factory([_fail("rate_limited"), _ok(), _ok()])
    out = _run(RecipeLadder(RECIPES, factory))
    assert out.success is False
    assert out.stopped_reason == "fatal"
    assert len(ran) == 1  # did not advance after fatal


def test_exhausts_all_recipes_without_success():
    factory, ran, seen = _scripted_factory(
        [_fail("phone_verification_failed")] * 3
    )
    out = _run(RecipeLadder(RECIPES, factory))
    assert out.success is False
    assert out.stopped_reason == "exhausted"
    assert len(ran) == 3
    assert len(out.attempts) == 3


def test_max_attempts_caps_the_ladder():
    factory, ran, seen = _scripted_factory(
        [_fail("phone_verification_failed")] * 3
    )
    out = _run(RecipeLadder(RECIPES, factory, max_attempts=2))
    assert out.success is False
    assert out.stopped_reason == "max_attempts"
    assert len(ran) == 2


def test_runner_exception_is_treated_as_recoverable_and_continues():
    class _Boom:
        async def run(self):
            raise RuntimeError("kaboom")

    results = [None, _ok()]

    def factory(recipe, index):
        if index == 0:
            return _Boom()
        return _FakeRunner(results[1], [])

    out = _run(RecipeLadder(RECIPES, factory))
    assert out.success is True
    assert out.attempts[0].result.failure_reason.startswith("agent_exception")
    assert out.attempts[0].decision == CONTINUE


def test_empty_recipes_rejected():
    with pytest.raises(ValueError):
        RecipeLadder([], lambda r, i: None)


# ---------------------------------------------------------------------------
# load_recipes / defaults
# ---------------------------------------------------------------------------


def test_default_ladder_leads_with_proven_austria():
    assert DEFAULT_LADDER[0].provider == "5sim"
    assert DEFAULT_LADDER[0].country == "austria"
    assert "virtual51" in DEFAULT_LADDER[0].operator


def test_load_recipes_from_json(tmp_path):
    p = tmp_path / "ladder.json"
    p.write_text(
        '[{"provider":"5sim","country":"austria","operator":"virtual51,any",'
        '"max_price":0.6,"sms_timeout":150,"label":"x"},'
        '{"provider":"smspool","country":"usa"}]',
        encoding="utf-8",
    )
    recipes = load_recipes(p)
    assert len(recipes) == 2
    assert recipes[0].label == "x"
    assert recipes[0].max_price == 0.6
    assert recipes[1].provider == "smspool"
    assert recipes[1].operator == "any"  # default


def test_load_recipes_rejects_non_list(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"provider":"5sim"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_recipes(p)


def test_recipe_describe_uses_label_then_falls_back():
    assert Recipe("5sim", "austria", label="LBL").describe() == "LBL"
    d = Recipe("5sim", "austria", "virtual51,any", 0.6).describe()
    assert "5sim" in d and "austria" in d and "virtual51" in d
