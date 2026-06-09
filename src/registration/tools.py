"""Custom MobileRun tools for Instagram registration + the run session.

The agent owns the phone-number lifecycle through three blocking custom tools:

- ``buy_phone_number(country=...)`` — buys an ``instagram`` activation number and
  records the live order on the session.
- ``get_sms_code()`` — polls the active order for the SMS code; on timeout the
  order is auto-cancelled (money guardrail) and released.
- ``ask_operator(question)`` — captures a screenshot/UI artifact, blocks for a
  human answer on stdin, and returns it to the agent (interactive flow dev).

``RegistrationSession`` owns the money guardrails so the policy lives in ONE
place (not scattered across the agent): exactly one active order at a time,
``finish`` on success, ``ban`` for a bad number, ``cancel`` on timeout/abort.

Everything that touches the outside world (5sim client, stdin, screenshot
capture) is injectable so unit tests never spend money or block on input.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.registration.five_sim import FiveSimClient, FiveSimError, FiveSimTimeout, FiveSimOrder
from src.worker.tools._types import ToolResult

OperatorInput = Callable[[str], str]
CaptureFn = Callable[[str], Awaitable[dict[str, Any]]]


class RegistrationSession:
    """Per-run state + 5sim money guardrails shared by the custom tools."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        country: str = "any",
        operator: str = "any",
        product: str = "instagram",
        max_price: float | None = None,
        code_timeout: float = 180.0,
        artifacts_dir: str | Path | None = None,
        operator_input: OperatorInput | None = None,
        capture_fn: CaptureFn | None = None,
    ) -> None:
        self._client = client or FiveSimClient()
        self._country = country
        self._operator = operator
        # Operator may be a comma-separated priority list (e.g. "o2,three,virtual34").
        # buy_phone_number tries them in order and takes the first that has stock,
        # so we use the highest-delivery-rate operator that is actually buyable.
        self._operators = [o.strip() for o in str(operator).split(",") if o.strip()] or ["any"]
        self._max_price = max_price
        self._product = product
        self._code_timeout = code_timeout
        self._artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self._operator_input = operator_input or input
        self._capture_fn = capture_fn
        self._order: FiveSimOrder | None = None
        self._ask_counter = 0

    # -- exposed state ------------------------------------------------------

    @property
    def active_order(self) -> FiveSimOrder | None:
        return self._order

    @property
    def phone_number(self) -> str | None:
        return self._order.phone if self._order else None

    @property
    def order_id(self) -> str | None:
        return str(self._order.id) if self._order else None

    # -- tools --------------------------------------------------------------

    async def buy_phone_number(self, country: str | None = None) -> ToolResult:
        """Buy an Instagram activation number; record it on the session.

        If an order is already active (e.g. Instagram rejected the previous number
        as invalid, before any SMS), release it first so the agent can retry with
        a fresh number — otherwise the single-active-order policy would deadlock.
        """
        if self._order is not None:
            await self._release("cancel")
        target_country = country or self._country
        errors: list[str] = []
        order = None
        used_operator = None
        for op in self._operators:
            try:
                order = await self._client.buy_number(
                    country=target_country,
                    operator=op,
                    product=self._product,
                    max_price=self._max_price,
                )
            except FiveSimError as exc:
                # Typically "no free phones" for that operator — fall through to
                # the next one in the priority list.
                errors.append(f"{op}: {exc}")
                continue
            used_operator = op
            break
        if order is None:
            tried = ", ".join(self._operators)
            return ToolResult.fail(
                f"5sim buy failed for {target_country} (tried operators: {tried}). "
                + " | ".join(errors)
            )
        self._order = order
        return ToolResult.ok(
            f"Bought phone {order.phone} (order_id={order.id}, "
            f"country={target_country}, operator={used_operator}). Use this number "
            f"in the signup form, then call get_sms_code to read the verification code."
        )

    async def get_sms_code(self) -> ToolResult:
        """Poll the active order for the SMS code; auto-cancel on timeout."""
        if self._order is None:
            return ToolResult.fail("no active order; call buy_phone_number first.")
        try:
            code = await self._client.get_code(
                self._order.id, timeout=self._code_timeout
            )
        except FiveSimTimeout as exc:
            await self._release("cancel")
            return ToolResult.fail(
                f"no SMS code within timeout ({exc}); order cancelled. "
                f"You may buy a new number."
            )
        except FiveSimError as exc:
            return ToolResult.fail(f"5sim check failed: {exc}")
        return ToolResult.ok(
            f"SMS code: {code.code} (from {code.sender or 'unknown'}). "
            f"Enter it in the verification field."
        )

    async def finish_order(self) -> ToolResult:
        """Mark the active order finished (code consumed OK)."""
        return await self._release("finish")

    async def ban_order(self) -> ToolResult:
        """Ban the active number (bad / already used)."""
        return await self._release("ban")

    async def cancel_order(self) -> ToolResult:
        """Cancel the active order (abort)."""
        return await self._release("cancel")

    async def ask_operator(self, question: str) -> ToolResult:
        """Capture artifacts, block for a human answer, return it to the agent."""
        self._ask_counter += 1
        artifact_note = ""
        if self._capture_fn is not None:
            try:
                info = await self._capture_fn(question)
                artifact_note = f" (artifacts: {info})"
            except Exception as exc:
                artifact_note = f" (capture failed: {exc})"

        prompt = (
            f"\n[ask_operator #{self._ask_counter}] {question}{artifact_note}\n"
            f"Your answer: "
        )
        try:
            answer = await asyncio.to_thread(self._operator_input, prompt)
        except Exception as exc:
            return ToolResult.fail(f"operator input failed: {exc}")
        return ToolResult.ok(f"Operator answered: {answer}")

    # -- internals ----------------------------------------------------------

    async def _release(self, how: str) -> ToolResult:
        if self._order is None:
            return ToolResult.ok("no active order to release.")
        order_id = self._order.id
        method = {
            "finish": self._client.finish,
            "ban": self._client.ban,
            "cancel": self._client.cancel,
        }[how]
        try:
            await method(order_id)
        except FiveSimError as exc:
            # Release the local handle anyway so we don't get stuck.
            self._order = None
            return ToolResult.fail(f"5sim {how} failed for {order_id}: {exc}")
        self._order = None
        return ToolResult.ok(f"order {order_id} {how}ed.")


def build_registration_tools(session: RegistrationSession) -> list[Callable]:
    """Return the agent-callable tools bound to ``session`` (list form).

    Kept for direct/unit use. The functions keep their stable ``__name__``
    (``buy_phone_number`` etc.).
    """

    async def buy_phone_number(country: str = "any") -> ToolResult:
        return await session.buy_phone_number(country=country)

    async def get_sms_code() -> ToolResult:
        return await session.get_sms_code()

    async def ask_operator(question: str) -> ToolResult:
        return await session.ask_operator(question)

    return [buy_phone_number, get_sms_code, ask_operator]


def _as_tool_string(result: ToolResult) -> str:
    """Coerce a ToolResult to the string MobileRun's registry expects.

    MobileRun treats a returned string starting with ``Failed`` as success=False
    (``tool_registry.execute``). ``ToolResult.fail`` already prefixes ``Failed:``.
    """
    if result.success:
        return result.message
    msg = result.message
    return msg if msg.startswith("Failed") else f"Failed: {msg}"


def build_custom_tools(session: RegistrationSession) -> dict:
    """Return MobileRun ``custom_tools`` dict bound to ``session``.

    Shape per ``mobilerun.agent.tool_registry.register_from_dict``:
        {name: {"function": callable, "parameters": {...}, "description": str}}
    Each function accepts a trailing ``ctx`` kwarg (MobileRun calls
    ``fn(**args, ctx=ctx)``) and returns a plain string for the agent to read.
    """

    async def buy_phone_number(country: str = "any", ctx=None) -> str:
        return _as_tool_string(await session.buy_phone_number(country=country))

    async def get_sms_code(ctx=None) -> str:
        return _as_tool_string(await session.get_sms_code())

    async def ask_operator(question: str, ctx=None) -> str:
        return _as_tool_string(await session.ask_operator(question))

    return {
        "buy_phone_number": {
            "function": buy_phone_number,
            "description": (
                "Buy a real phone number for Instagram SMS verification. Call when "
                "the signup flow asks for a phone number; enter the returned number."
            ),
            "parameters": {
                "country": {
                    "type": "string",
                    "required": False,
                    "default": "any",
                    "description": "5sim country code, or 'any'.",
                },
            },
        },
        "get_sms_code": {
            "function": get_sms_code,
            "description": (
                "Poll for the SMS verification code on the active phone number. "
                "Blocks until the code arrives or times out. Call after Instagram "
                "says it sent a code."
            ),
            "parameters": {},
        },
        "ask_operator": {
            "function": ask_operator,
            "description": (
                "Ask the human operator for help on an unexpected screen. Blocks "
                "until the operator answers. Use when stuck, on a captcha/challenge, "
                "or any screen not covered by your instructions."
            ),
            "parameters": {
                "question": {
                    "type": "string",
                    "required": True,
                    "description": "Clear description of what you see + your question.",
                },
            },
        },
    }


__all__ = [
    "RegistrationSession",
    "build_registration_tools",
    "build_custom_tools",
]
