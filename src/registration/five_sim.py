"""5sim.net API client for phone-number activation.

A small, dependency-free client (stdlib ``urllib`` only) used by the
registration custom tools to buy a disposable phone number, poll for the SMS
verification code, and close the order out (``finish`` on success, ``ban`` for a
bad number, ``cancel`` to abort and refund).

Design notes (matching repo conventions):
- No third-party HTTP dep. The real network call goes through ``_urllib_transport``
  wrapped in ``asyncio.to_thread`` so the public API stays ``async`` (mirrors
  ``src/worker/tools/_adb.py``).
- The transport is **injectable** (``transport=`` on the client) so unit tests
  pass a fake and never touch the network or spend money.
- ``FIVESIM_API_KEY`` is read from the environment when no key is passed.

5sim REST surface used (Bearer auth, see the design spec §3):

    GET /user/profile                                   balance / profile
    GET /user/buy/activation/{country}/{operator}/{product}   buy a number
    GET /user/check/{id}                                poll for SMS
    GET /user/finish/{id}                               consumed OK
    GET /user/ban/{id}                                  bad number
    GET /user/cancel/{id}                               abort / refund
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_DEFAULT_BASE_URL = "https://5sim.net/v1"
_DEFAULT_PRODUCT = "instagram"
_DEFAULT_OPERATOR = "any"
_DEFAULT_COUNTRY = "any"
_DEFAULT_TIMEOUT = 180.0
_DEFAULT_POLL_INTERVAL = 5.0
_HTTP_TIMEOUT = 30

# Transport contract: (method, url, headers, timeout_seconds) -> (status, body).
Transport = Callable[[str, str, dict[str, str], int], "tuple[int, str]"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FiveSimError(RuntimeError):
    """Any 5sim API failure (HTTP error or a plaintext error body)."""


class FiveSimTimeout(FiveSimError):
    """``get_code`` exhausted its polling window without an SMS code."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmsCode:
    """One SMS message parsed from an order's ``sms`` array."""

    code: str
    text: str = ""
    sender: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FiveSimOrder:
    """A 5sim activation order.

    ``sms`` is the (possibly empty) list of received messages; it is populated
    by ``buy_number`` (usually empty) and ``check_order`` (filled once the code
    arrives).
    """

    id: int
    phone: str
    operator: str = ""
    product: str = ""
    status: str = ""
    price: float | None = None
    country: str = ""
    sms: list[SmsCode] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def first_code(self) -> SmsCode | None:
        return self.sms[0] if self.sms else None


# ---------------------------------------------------------------------------
# Default (real) transport
# ---------------------------------------------------------------------------


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], timeout: int
) -> tuple[int, str]:
    """Blocking HTTP via stdlib ``urllib``. Returns ``(status, body_text)``.

    Never raises for an HTTP status code — a 4xx/5xx is returned as
    ``(status, body)`` so the client maps it to ``FiveSimError`` with the
    server's message intact (5sim puts useful text in error bodies).
    """
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except urllib.error.URLError as exc:
        raise FiveSimError(f"network error: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FiveSimClient:
    """Async wrapper over the 5sim REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        transport: Transport | None = None,
    ) -> None:
        key = api_key or os.environ.get("FIVESIM_API_KEY")
        if not key:
            raise FiveSimError(
                "FIVESIM_API_KEY is not set (pass api_key= or export the env var)."
            )
        self._api_key = key
        self._base_url = base_url.rstrip("/")
        self._transport: Transport = transport or _urllib_transport

    @property
    def api_key(self) -> str:
        return self._api_key

    # -- public API ---------------------------------------------------------

    async def balance(self) -> float:
        """Return the account balance from ``GET /user/profile``."""
        data = await self._get_json("/user/profile")
        try:
            return float(data["balance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FiveSimError(f"unexpected profile response: {data!r}") from exc

    async def buy_number(
        self,
        country: str = _DEFAULT_COUNTRY,
        operator: str = _DEFAULT_OPERATOR,
        product: str = _DEFAULT_PRODUCT,
    ) -> FiveSimOrder:
        """Buy an activation number for ``product`` (default: instagram)."""
        path = f"/user/buy/activation/{country}/{operator}/{product}"
        data = await self._get_json(path)
        return _parse_order(data)

    async def check_order(self, order_id: int | str) -> FiveSimOrder:
        """Fetch current order state (including any received SMS)."""
        data = await self._get_json(f"/user/check/{order_id}")
        return _parse_order(data)

    async def get_code(
        self,
        order_id: int | str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        time_fn: Callable[[], float] | None = None,
    ) -> SmsCode:
        """Poll ``check_order`` until an SMS code arrives or ``timeout`` elapses.

        Money guardrail: this does NOT cancel the order on timeout — the caller
        (the custom tool / session) owns ``cancel``/``ban`` so the policy lives
        in one place. ``sleep`` and ``time_fn`` are injectable for deterministic
        tests.
        """
        clock = time_fn or _monotonic
        deadline = clock() + timeout
        while True:
            order = await self.check_order(order_id)
            code = order.first_code
            if code is not None:
                return code
            if clock() >= deadline:
                raise FiveSimTimeout(
                    f"no SMS for order {order_id} within {timeout:.0f}s "
                    f"(last status={order.status!r})"
                )
            await sleep(poll_interval)

    async def finish(self, order_id: int | str) -> FiveSimOrder:
        """Mark the order finished (code consumed successfully)."""
        return _parse_order(await self._get_json(f"/user/finish/{order_id}"))

    async def ban(self, order_id: int | str) -> FiveSimOrder:
        """Ban the number (bad / already used) — blocks reuse, may refund."""
        return _parse_order(await self._get_json(f"/user/ban/{order_id}"))

    async def cancel(self, order_id: int | str) -> FiveSimOrder:
        """Cancel the order (no SMS / abort) — refunds if no SMS was received."""
        return _parse_order(await self._get_json(f"/user/cancel/{order_id}"))

    # -- internals ----------------------------------------------------------

    async def _get_json(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        status, body = await asyncio.to_thread(
            self._transport, "GET", url, headers, _HTTP_TIMEOUT
        )
        return _parse_response(status, body, url)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_response(status: int, body: str, url: str) -> Any:
    """Validate status + body and return parsed JSON.

    5sim sometimes returns a plaintext error (``no free phones``, ``not enough
    user balance``, ``order not found``) with a 200 or 4xx — anything that is
    not a JSON object/array is treated as an error.
    """
    text = (body or "").strip()
    if status >= 400:
        raise FiveSimError(f"5sim {status} for {url}: {text or '(empty body)'}")
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise FiveSimError(f"5sim error for {url}: {text or '(empty body)'}") from exc
    if not isinstance(data, (dict, list)):
        raise FiveSimError(f"5sim error for {url}: {text}")
    return data


def _parse_order(data: Any) -> FiveSimOrder:
    if not isinstance(data, dict):
        raise FiveSimError(f"unexpected order payload: {data!r}")
    sms = [_parse_sms(item) for item in (data.get("sms") or []) if isinstance(item, dict)]
    price = data.get("price")
    return FiveSimOrder(
        id=int(data.get("id", 0)),
        phone=str(data.get("phone", "")),
        operator=str(data.get("operator", "")),
        product=str(data.get("product", "")),
        status=str(data.get("status", "")),
        price=float(price) if isinstance(price, (int, float)) else None,
        country=str(data.get("country", "")),
        sms=sms,
        raw=data,
    )


def _parse_sms(item: dict[str, Any]) -> SmsCode:
    return SmsCode(
        code=str(item.get("code", "")),
        text=str(item.get("text", "")),
        sender=str(item.get("sender", "")),
        raw=item,
    )


def _monotonic() -> float:
    import time

    return time.monotonic()


__all__ = [
    "FiveSimClient",
    "FiveSimError",
    "FiveSimTimeout",
    "FiveSimOrder",
    "SmsCode",
]
