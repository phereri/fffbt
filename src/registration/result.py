"""Result types for the Instagram registration runner.

``RegistrationResult`` is a dependency-free dataclass so unit tests do not need
pydantic installed. The Pydantic schema used as the agent's structured-output
target is built lazily in ``registration_result_pydantic_model`` and only touched
when a real ``MobileAgent`` is constructed (mirrors
``agent_runner.mobilerun_agent_runner._post_result_pydantic_model``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RegistrationResult:
    """Structured outcome of one registration attempt.

    Field names mirror the lazy Pydantic ``RegistrationOutput`` model so an
    agent's ``structured_output`` can be copied attribute-for-attribute via
    ``from_structured``.
    """

    success: bool
    username: str | None = None
    password: str | None = None
    full_name: str | None = None
    birthday: str | None = None
    phone_number: str | None = None
    phone_country: str | None = None
    fivesim_order_id: str | None = None
    failure_reason: str | None = None
    notes: str | None = None

    @classmethod
    def from_structured(cls, obj: Any) -> "RegistrationResult | None":
        if obj is None:
            return None
        return cls(
            success=bool(_attr(obj, "success", False)),
            username=_optional_str(_attr(obj, "username", None)),
            password=_optional_str(_attr(obj, "password", None)),
            full_name=_optional_str(_attr(obj, "full_name", None)),
            birthday=_optional_str(_attr(obj, "birthday", None)),
            phone_number=_optional_str(_attr(obj, "phone_number", None)),
            phone_country=_optional_str(_attr(obj, "phone_country", None)),
            fivesim_order_id=_optional_str(_attr(obj, "fivesim_order_id", None)),
            failure_reason=_optional_str(_attr(obj, "failure_reason", None)),
            notes=_optional_str(_attr(obj, "notes", None)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "username": self.username,
            "password": self.password,
            "full_name": self.full_name,
            "birthday": self.birthday,
            "phone_number": self.phone_number,
            "phone_country": self.phone_country,
            "fivesim_order_id": self.fivesim_order_id,
            "failure_reason": self.failure_reason,
            "notes": self.notes,
        }


def registration_result_pydantic_model() -> type:
    """Construct the Pydantic ``RegistrationOutput`` model — lazily.

    Imported only when a real ``MobileAgent`` is built so the module stays
    importable without pydantic installed (CI / Mac dev).
    """
    from pydantic import BaseModel, Field

    class RegistrationOutput(BaseModel):
        success: bool = Field(
            description="True iff a usable Instagram account was created."
        )
        username: str | None = Field(
            default=None, description="The username the agent chose."
        )
        password: str | None = Field(
            default=None, description="The password the agent set."
        )
        full_name: str | None = Field(default=None)
        birthday: str | None = Field(
            default=None, description="ISO date YYYY-MM-DD; account holder is 18+."
        )
        phone_number: str | None = Field(
            default=None, description="E.164 phone used for verification."
        )
        phone_country: str | None = Field(default=None)
        fivesim_order_id: str | None = Field(default=None)
        failure_reason: str | None = Field(
            default=None,
            description="Machine-friendly reason when success is False.",
        )
        notes: str | None = Field(
            default=None, description="Free-form notes / unexpected screens seen."
        )

    return RegistrationOutput


def _attr(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = [
    "RegistrationResult",
    "registration_result_pydantic_model",
]
