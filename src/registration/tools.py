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
        code_timeout: float = 180.0,
        artifacts_dir: str | Path | None = None,
        operator_input: OperatorInput | None = None,
        capture_fn: CaptureFn | None = None,
    ) -> None:
        self._client = client or FiveSimClient()
        self._country = country
        self._operator = operator
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
        """Buy an Instagram activation number; record it on the session."""
        if self._order is not None:
            return ToolResult.fail(
                f"an active order already exists (id={self._order.id}, "
                f"phone={self._order.phone}); release it before buying another."
            )
        try:
            order = await self._client.buy_number(
                country=country or self._country,
                operator=self._operator,
                product=self._product,
            )
        except FiveSimError as exc:
            return ToolResult.fail(f"5sim buy failed: {exc}")
        self._order = order
        return ToolResult.ok(
            f"Bought phone {order.phone} (order_id={order.id}, country="
            f"{country or self._country}). Use this number in the signup form, "
            f"then call get_sms_code to read the verification code."
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
    """Return the agent-callable tools bound to ``session``.

    The functions keep their stable ``__name__`` (``buy_phone_number`` etc.) so
    MobileRun registers them under the names the goal references.
    """

    async def buy_phone_number(country: str = "any") -> ToolResult:
        return await session.buy_phone_number(country=country)

    async def get_sms_code() -> ToolResult:
        return await session.get_sms_code()

    async def ask_operator(question: str) -> ToolResult:
        return await session.ask_operator(question)

    return [buy_phone_number, get_sms_code, ask_operator]


__all__ = [
    "RegistrationSession",
    "build_registration_tools",
]
