"""SMSPool API client — a drop-in alternative to ``FiveSimClient``.

SMSPool sells non-VoIP / real-SIM numbers (better Instagram deliverability,
especially for the re-verification challenge that VoIP numbers fail). This client
implements the same surface ``RegistrationSession`` uses — ``buy_number``,
``get_code``, ``cancel``/``ban``/``finish``, ``balance`` — and reuses the
``FiveSimError`` / ``FiveSimTimeout`` / ``FiveSimOrder`` / ``SmsCode`` types so it
is interchangeable behind the same interface (see [[autoreg-status]]).

API (native SMSPool, POST form-encoded, key in body):
    POST /request/balance               -> {"balance": "1.23"}
    POST /purchase/sms  country,service -> {"success":1,"order_id":..,"number":"84..","cc":"84","phonenumber":".."}
    POST /sms/check     orderid         -> {"status":<int>,"sms":"123456",...}
    POST /sms/cancel    orderid         -> {"success":1}
    POST /sms/resend    orderid         -> resend the SMS to the same number

Country/service are numeric IDs (Vietnam=11, USA=1; Instagram/Threads=457).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from src.registration.five_sim import (
    FiveSimError,
    FiveSimOrder,
    FiveSimTimeout,
    SmsCode,
)

_DEFAULT_BASE_URL = "https://api.smspool.net"
_DEFAULT_TIMEOUT = 180.0
_DEFAULT_POLL_INTERVAL = 4.0

# Country name -> SMSPool country ID (extend as needed; numeric ids pass through).
COUNTRY_IDS: dict[str, int] = {
    "vietnam": 11,
    "vn": 11,
    "usa": 1,
    "us": 1,
    "united states": 1,
    "any": 11,  # default to Vietnam for "any"
}
# Product -> SMSPool service ID.
SERVICE_IDS: dict[str, int] = {
    "instagram": 457,  # "Instagram / Threads"
}

# SMSPool /sms/check status codes (observed): 1=pending, 3=completed, 6=refunded/expired.
_STATUS_DONE = {3}
_STATUS_DEAD = {6}

PostTransport = Callable[[str, dict[str, Any]], "tuple[int, str]"]


def _urllib_post(url: str, data: dict[str, Any], timeout: int = 30) -> tuple[int, str]:
    body = urllib.parse.urlencode({k: v for k, v in data.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise FiveSimError(f"network error: {exc.reason}") from exc


def _country_id(country: str | int | None) -> int:
    if country is None:
        return COUNTRY_IDS["any"]
    if isinstance(country, int) or str(country).isdigit():
        return int(country)
    return COUNTRY_IDS.get(str(country).strip().lower(), COUNTRY_IDS["any"])


def _service_id(product: str | int | None) -> int:
    if product is None:
        return SERVICE_IDS["instagram"]
    if isinstance(product, int) or str(product).isdigit():
        return int(product)
    return SERVICE_IDS.get(str(product).strip().lower(), SERVICE_IDS["instagram"])


class SmsPoolClient:
    """Async wrapper over the SMSPool REST API (same surface as FiveSimClient)."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        transport: PostTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("SMSPOOL_API_KEY") or os.environ.get("SMSPOOL_KEY")
        if not key:
            raise FiveSimError("SMSPOOL_API_KEY is not set (pass api_key= or export the env var).")
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._transport: PostTransport = transport or _urllib_post

    @property
    def api_key(self) -> str:
        return self._api_key

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        payload = {"key": self._api_key, **data}
        status, body = self._transport(f"{self._base_url}{path}", payload)
        try:
            parsed = json.loads(body)
        except Exception as exc:
            raise FiveSimError(f"smspool non-JSON {status} for {path}: {body[:120]!r}") from exc
        if status >= 400:
            raise FiveSimError(f"smspool {status} for {path}: {parsed!r}")
        return parsed

    # -- public API ---------------------------------------------------------

    async def balance(self) -> float:
        data = await asyncio.to_thread(self._post, "/request/balance", {})
        try:
            return float(data["balance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FiveSimError(f"unexpected balance response: {data!r}") from exc

    async def buy_number(
        self,
        country: str = "vietnam",
        operator: str = "any",  # unused by SMSPool (kept for interface parity)
        product: str = "instagram",
        max_price: float | None = None,
    ) -> FiveSimOrder:
        data = {
            "country": _country_id(country),
            "service": _service_id(product),
        }
        if max_price is not None:
            data["max_price"] = max_price
        resp = await asyncio.to_thread(self._post, "/purchase/sms", data)
        if not (resp.get("success") in (1, "1", True) or resp.get("order_id") or resp.get("orderid")):
            raise FiveSimError(f"smspool buy failed: {resp.get('message') or resp}")
        oid = resp.get("order_id") or resp.get("orderid") or resp.get("id")
        cc = str(resp.get("cc") or "")
        local = str(resp.get("phonenumber") or "")
        full = str(resp.get("number") or (cc + local))
        phone = "+" + full.lstrip("+")
        return FiveSimOrder(
            id=oid, phone=phone, operator="smspool",
            product=str(product), country=str(country), status="PENDING", raw=resp,
        )

    async def check_order(self, order_id: int | str) -> FiveSimOrder:
        resp = await asyncio.to_thread(self._post, "/sms/check", {"orderid": order_id})
        code = resp.get("sms") or resp.get("code")
        sms = [SmsCode(code=str(code), text=str(resp.get("full_sms") or ""), raw=resp)] if code else []
        return FiveSimOrder(id=order_id, phone="", status=str(resp.get("status", "")), sms=sms, raw=resp)

    async def get_code(
        self,
        order_id: int | str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> SmsCode:
        deadline = time.monotonic() + timeout
        while True:
            resp = await asyncio.to_thread(self._post, "/sms/check", {"orderid": order_id})
            code = resp.get("sms") or resp.get("code")
            if code:
                return SmsCode(code=str(code), text=str(resp.get("full_sms") or ""), raw=resp)
            try:
                st = int(resp.get("status"))
            except (TypeError, ValueError):
                st = None
            if st in _STATUS_DEAD:
                raise FiveSimError(f"smspool order {order_id} dead (status={st}): {resp}")
            if time.monotonic() >= deadline:
                raise FiveSimTimeout(f"no SMS code for order {order_id} within {timeout:.0f}s")
            await asyncio.sleep(poll_interval)

    async def cancel(self, order_id: int | str) -> FiveSimOrder:
        resp = await asyncio.to_thread(self._post, "/sms/cancel", {"orderid": order_id})
        return FiveSimOrder(id=order_id, phone="", status="CANCELED", raw=resp)

    # SMSPool has no "ban"; cancel refunds if no code was received.
    async def ban(self, order_id: int | str) -> FiveSimOrder:
        return await self.cancel(order_id)

    # SMSPool auto-completes/charges on code receipt; nothing to "finish".
    async def finish(self, order_id: int | str) -> FiveSimOrder:
        return FiveSimOrder(id=order_id, phone="", status="FINISHED")


__all__ = ["SmsPoolClient", "COUNTRY_IDS", "SERVICE_IDS"]
