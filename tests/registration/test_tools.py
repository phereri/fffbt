"""Tests for the registration custom tools + session (``tools.py``).

No real device, no network, no stdin. The 5sim client and the operator prompt
are injected fakes. Verifies the money guardrails (finish on success, cancel on
timeout, ban on bad number), single-active-order policy, and ask_operator
capture + blocking-read contract.
"""

from __future__ import annotations

import asyncio

import pytest

from src.registration.five_sim import FiveSimOrder, FiveSimTimeout, SmsCode
from src.registration.tools import (
    RegistrationSession,
    build_registration_tools,
)


class _FakeClient:
    """Scriptable stand-in for FiveSimClient."""

    def __init__(self):
        self.bought: list[tuple] = []
        self.finished: list = []
        self.cancelled: list = []
        self.banned: list = []
        self._buy = FiveSimOrder(id=555, phone="+790111", status="PENDING", product="instagram")
        self._code = SmsCode(code="123456", sender="Instagram", text="code 123456")
        self.get_code_raises: Exception | None = None

    async def buy_number(self, country="any", operator="any", product="instagram"):
        self.bought.append((country, operator, product))
        return self._buy

    async def get_code(self, order_id, **kw):
        if self.get_code_raises:
            raise self.get_code_raises
        return self._code

    async def finish(self, order_id):
        self.finished.append(order_id)
        return FiveSimOrder(id=int(order_id), phone="+790111", status="FINISHED")

    async def cancel(self, order_id):
        self.cancelled.append(order_id)
        return FiveSimOrder(id=int(order_id), phone="+790111", status="CANCELED")

    async def ban(self, order_id):
        self.banned.append(order_id)
        return FiveSimOrder(id=int(order_id), phone="+790111", status="BANNED")


def _session(**kw) -> tuple[RegistrationSession, _FakeClient]:
    client = _FakeClient()
    sess = RegistrationSession(client=client, country=kw.pop("country", "any"), **kw)
    return sess, client


# ---------------------------------------------------------------------------
# buy_phone_number
# ---------------------------------------------------------------------------


class TestBuyPhoneNumber:
    def test_buys_and_records_order(self):
        sess, client = _session()
        res = asyncio.run(sess.buy_phone_number())
        assert "+790111" in res.message
        assert res.success
        assert client.bought == [("any", "any", "instagram")]
        assert sess.active_order is not None
        assert sess.active_order.id == 555

    def test_country_override(self):
        sess, client = _session()
        asyncio.run(sess.buy_phone_number(country="england"))
        assert client.bought[0][0] == "england"

    def test_second_buy_without_release_is_rejected(self):
        sess, _ = _session()
        asyncio.run(sess.buy_phone_number())
        res = asyncio.run(sess.buy_phone_number())
        assert not res.success
        assert "active order" in res.message.lower()


# ---------------------------------------------------------------------------
# get_sms_code
# ---------------------------------------------------------------------------


class TestGetSmsCode:
    def test_returns_code(self):
        sess, _ = _session()
        asyncio.run(sess.buy_phone_number())
        res = asyncio.run(sess.get_sms_code())
        assert res.success
        assert "123456" in res.message

    def test_without_order_fails(self):
        sess, _ = _session()
        res = asyncio.run(sess.get_sms_code())
        assert not res.success
        assert "no active order" in res.message.lower()

    def test_timeout_auto_cancels(self):
        sess, client = _session()
        asyncio.run(sess.buy_phone_number())
        client.get_code_raises = FiveSimTimeout("no sms")
        res = asyncio.run(sess.get_sms_code())
        assert not res.success
        assert client.cancelled == [555]
        # Order released after cancel so a retry buy is allowed.
        assert sess.active_order is None


# ---------------------------------------------------------------------------
# lifecycle: finish / ban
# ---------------------------------------------------------------------------


class TestRelease:
    def test_finish_on_success(self):
        sess, client = _session()
        asyncio.run(sess.buy_phone_number())
        asyncio.run(sess.finish_order())
        assert client.finished == [555]
        assert sess.active_order is None

    def test_ban_bad_number(self):
        sess, client = _session()
        asyncio.run(sess.buy_phone_number())
        asyncio.run(sess.ban_order())
        assert client.banned == [555]
        assert sess.active_order is None

    def test_finish_without_order_is_noop(self):
        sess, client = _session()
        asyncio.run(sess.finish_order())
        assert client.finished == []


# ---------------------------------------------------------------------------
# ask_operator
# ---------------------------------------------------------------------------


class TestAskOperator:
    def test_blocks_and_returns_answer(self, tmp_path):
        answers = iter(["tap the blue button"])

        def fake_input(prompt: str) -> str:
            return next(answers)

        captured = []

        async def fake_capture(question: str) -> dict:
            captured.append(question)
            return {"screenshot": str(tmp_path / "s.png")}

        sess, _ = _session(
            artifacts_dir=str(tmp_path),
            operator_input=fake_input,
            capture_fn=fake_capture,
        )
        res = asyncio.run(sess.ask_operator("What screen is this?"))
        assert res.success
        assert "tap the blue button" in res.message
        assert captured == ["What screen is this?"]


# ---------------------------------------------------------------------------
# build_registration_tools
# ---------------------------------------------------------------------------


class TestBuildTools:
    def test_returns_three_callables_bound_to_session(self):
        sess, _ = _session()
        tools = build_registration_tools(sess)
        names = {t.__name__ for t in tools}
        assert names == {"buy_phone_number", "get_sms_code", "ask_operator"}

    def test_tools_invoke_session(self):
        sess, client = _session()
        tools = {t.__name__: t for t in build_registration_tools(sess)}
        asyncio.run(tools["buy_phone_number"]())
        assert client.bought
