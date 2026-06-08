"""Tests for the 5sim.net API client.

All HTTP is mocked via an injected fake transport — no real network, no real
spend. The transport contract is ``(method, url, headers, timeout) -> (status,
body)`` where ``body`` is the raw response text (JSON or 5sim plaintext error).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.registration.five_sim import (
    FiveSimClient,
    FiveSimError,
    FiveSimOrder,
    FiveSimTimeout,
    SmsCode,
)


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records requests and replays a scripted sequence of responses.

    ``responses`` is a list of ``(status, body)`` tuples consumed in order; if
    exhausted, the last entry is repeated. ``body`` may be a dict (auto-JSON-
    encoded) or a raw string (for 5sim plaintext errors).
    """

    def __init__(self, responses: list[tuple[int, Any]]):
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "timeout": timeout}
        )
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        status, body = self._responses[idx]
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        return status, body

    @property
    def last_url(self) -> str:
        return self.calls[-1]["url"]

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


_BUY_OK = {
    "id": 11631253,
    "phone": "+79000381454",
    "operator": "beeline",
    "product": "instagram",
    "price": 21,
    "status": "PENDING",
    "country": "russia",
    "sms": None,
}

_CHECK_PENDING = {**_BUY_OK, "status": "PENDING", "sms": []}
_CHECK_RECEIVED = {
    **_BUY_OK,
    "status": "RECEIVED",
    "sms": [
        {
            "created_at": "2026-06-08T10:00:00.000Z",
            "date": "2026-06-08T10:00:00.000Z",
            "sender": "Instagram",
            "text": "Your Instagram code is 552 244",
            "code": "552244",
        }
    ],
}


def _client(responses: list[tuple[int, Any]], **kw) -> tuple[FiveSimClient, _FakeTransport]:
    transport = _FakeTransport(responses)
    client = FiveSimClient(api_key="test-key", transport=transport, **kw)
    return client, transport


# ---------------------------------------------------------------------------
# Construction / auth
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("FIVESIM_API_KEY", "env-key")
        client = FiveSimClient(transport=_FakeTransport([(200, {})]))
        assert client.api_key == "env-key"

    def test_explicit_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("FIVESIM_API_KEY", "env-key")
        client = FiveSimClient(api_key="explicit", transport=_FakeTransport([(200, {})]))
        assert client.api_key == "explicit"

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("FIVESIM_API_KEY", raising=False)
        with pytest.raises(FiveSimError):
            FiveSimClient(transport=_FakeTransport([(200, {})]))

    def test_sends_bearer_auth_header(self):
        client, transport = _client([(200, {"balance": 1.0})])
        asyncio.run(client.balance())
        headers = transport.calls[-1]["headers"]
        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Accept"] == "application/json"


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------


class TestBalance:
    def test_parses_balance(self):
        client, transport = _client([(200, {"id": 1, "email": "a@b.c", "balance": 42.5})])
        assert asyncio.run(client.balance()) == 42.5
        assert transport.last_url.endswith("/user/profile")

    def test_http_error_raises(self):
        client, _ = _client([(401, "unauthorized")])
        with pytest.raises(FiveSimError):
            asyncio.run(client.balance())


# ---------------------------------------------------------------------------
# buy_number
# ---------------------------------------------------------------------------


class TestBuyNumber:
    def test_default_url_and_parse(self):
        client, transport = _client([(200, _BUY_OK)])
        order = asyncio.run(client.buy_number())
        assert isinstance(order, FiveSimOrder)
        assert order.id == 11631253
        assert order.phone == "+79000381454"
        assert order.product == "instagram"
        assert transport.last_url.endswith("/user/buy/activation/any/any/instagram")

    def test_custom_country_operator_product(self):
        client, transport = _client([(200, _BUY_OK)])
        asyncio.run(
            client.buy_number(country="england", operator="virtual21", product="instagram")
        )
        assert transport.last_url.endswith(
            "/user/buy/activation/england/virtual21/instagram"
        )

    def test_no_free_phones_plaintext_raises(self):
        client, _ = _client([(400, "no free phones")])
        with pytest.raises(FiveSimError) as ei:
            asyncio.run(client.buy_number())
        assert "no free phones" in str(ei.value)

    def test_not_enough_balance_plaintext_raises(self):
        # 5sim historically returns 200 + plaintext for this — must still raise.
        client, _ = _client([(200, "not enough user balance")])
        with pytest.raises(FiveSimError):
            asyncio.run(client.buy_number())


# ---------------------------------------------------------------------------
# check_order
# ---------------------------------------------------------------------------


class TestCheckOrder:
    def test_parses_sms(self):
        client, transport = _client([(200, _CHECK_RECEIVED)])
        order = asyncio.run(client.check_order(11631253))
        assert order.status == "RECEIVED"
        assert len(order.sms) == 1
        assert order.sms[0].code == "552244"
        assert transport.last_url.endswith("/user/check/11631253")

    def test_pending_has_no_sms(self):
        client, _ = _client([(200, _CHECK_PENDING)])
        order = asyncio.run(client.check_order(11631253))
        assert order.sms == []


# ---------------------------------------------------------------------------
# get_code (polling)
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic injectable sleep that advances a monotonic counter."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds

    def time(self) -> float:
        return self.now


class TestGetCode:
    def test_immediate_code(self):
        client, _ = _client([(200, _CHECK_RECEIVED)])
        clock = _Clock()
        code = asyncio.run(
            client.get_code(11631253, sleep=clock.sleep, time_fn=clock.time)
        )
        assert isinstance(code, SmsCode)
        assert code.code == "552244"
        assert clock.sleeps == []  # no polling needed

    def test_polls_then_receives(self):
        client, transport = _client(
            [(200, _CHECK_PENDING), (200, _CHECK_PENDING), (200, _CHECK_RECEIVED)]
        )
        clock = _Clock()
        code = asyncio.run(
            client.get_code(
                11631253, poll_interval=5.0, sleep=clock.sleep, time_fn=clock.time
            )
        )
        assert code.code == "552244"
        assert len(transport.calls) == 3
        assert clock.sleeps == [5.0, 5.0]

    def test_times_out(self):
        client, _ = _client([(200, _CHECK_PENDING)])
        clock = _Clock()
        with pytest.raises(FiveSimTimeout):
            asyncio.run(
                client.get_code(
                    11631253,
                    timeout=12.0,
                    poll_interval=5.0,
                    sleep=clock.sleep,
                    time_fn=clock.time,
                )
            )


# ---------------------------------------------------------------------------
# lifecycle: finish / cancel / ban
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_finish(self):
        client, transport = _client([(200, {**_BUY_OK, "status": "FINISHED"})])
        order = asyncio.run(client.finish(11631253))
        assert order.status == "FINISHED"
        assert transport.last_url.endswith("/user/finish/11631253")

    def test_cancel(self):
        client, transport = _client([(200, {**_BUY_OK, "status": "CANCELED"})])
        order = asyncio.run(client.cancel(11631253))
        assert order.status == "CANCELED"
        assert transport.last_url.endswith("/user/cancel/11631253")

    def test_ban(self):
        client, transport = _client([(200, {**_BUY_OK, "status": "BANNED"})])
        order = asyncio.run(client.ban(11631253))
        assert order.status == "BANNED"
        assert transport.last_url.endswith("/user/ban/11631253")

    def test_cancel_http_error_raises(self):
        client, _ = _client([(500, "internal error")])
        with pytest.raises(FiveSimError):
            asyncio.run(client.cancel(11631253))
