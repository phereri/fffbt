"""Tests for SmsPoolClient (mocked transport — no real network/spend)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.registration.five_sim import FiveSimError, FiveSimTimeout
from src.registration.sms_pool import SmsPoolClient, _country_id, _service_id


class _FakeTransport:
    """Scriptable POST transport: maps path-suffix -> queue of (status, json)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, list] = {}

    def __call__(self, url, data):
        self.calls.append((url, data))
        path = url.split("smspool.net", 1)[-1]
        q = self.responses.get(path)
        item = q.pop(0) if q else (200, {})
        status, body = item
        return status, json.dumps(body)


def _client(ft):
    return SmsPoolClient(api_key="k", transport=ft)


def test_country_and_service_mapping():
    assert _country_id("vietnam") == 11
    assert _country_id("VN") == 11
    assert _country_id("usa") == 1
    assert _country_id(11) == 11
    assert _country_id("any") == 11  # default VN
    assert _service_id("instagram") == 457
    assert _service_id(457) == 457


def test_balance():
    ft = _FakeTransport(); ft.responses["/request/balance"] = [(200, {"balance": "3.50"})]
    assert asyncio.run(_client(ft).balance()) == 3.50


def test_buy_builds_full_number_and_passes_ids():
    ft = _FakeTransport()
    ft.responses["/purchase/sms"] = [(200, {"success": 1, "order_id": "999", "cc": "84", "phonenumber": "987654321"})]
    order = asyncio.run(_client(ft).buy_number(country="vietnam", product="instagram", max_price=0.04))
    assert order.id == "999"
    assert order.phone == "+84987654321"
    # the request carried the numeric country/service ids + key + max_price
    _, data = ft.calls[0]
    assert data["country"] == 11 and data["service"] == 457
    assert data["key"] == "k" and data["max_price"] == 0.04


def test_buy_failure_raises():
    ft = _FakeTransport(); ft.responses["/purchase/sms"] = [(200, {"success": 0, "message": "no stock"})]
    with pytest.raises(FiveSimError):
        asyncio.run(_client(ft).buy_number(country="vietnam"))


def test_get_code_polls_until_received():
    ft = _FakeTransport()
    ft.responses["/sms/check"] = [
        (200, {"status": 1}),               # pending
        (200, {"status": 3, "sms": "123456", "full_sms": "Your code is 123456"}),
    ]
    code = asyncio.run(_client(ft).get_code("999", timeout=5, poll_interval=0))
    assert code.code == "123456"


def test_get_code_timeout():
    ft = _FakeTransport(); ft.responses["/sms/check"] = [(200, {"status": 1})] * 50
    with pytest.raises(FiveSimTimeout):
        asyncio.run(_client(ft).get_code("999", timeout=0, poll_interval=0))


def test_get_code_dead_status_raises():
    ft = _FakeTransport(); ft.responses["/sms/check"] = [(200, {"status": 6})]
    with pytest.raises(FiveSimError):
        asyncio.run(_client(ft).get_code("999", timeout=5, poll_interval=0))


def test_cancel_and_finish():
    ft = _FakeTransport(); ft.responses["/sms/cancel"] = [(200, {"success": 1})]
    assert asyncio.run(_client(ft).cancel("999")).status == "CANCELED"
    assert asyncio.run(_client(ft).finish("999")).status == "FINISHED"  # no network call
