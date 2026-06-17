"""Result types for the MobileRun agent runner.

These dataclasses are dependency-free so unit tests do not need pydantic or
mobilerun installed. The Pydantic ``PostResult`` schema used as the agent's
structured-output target is built lazily inside
``mobilerun_agent_runner._post_result_pydantic_model`` and only touched when
a real ``MobileAgent`` is constructed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResultCategory(str, Enum):
    """Coarse outcome category used to drive ``StepStatus`` mapping."""

    OK = "ok"
    HARD_STOP = "hard_stop"
    NEEDS_REVIEW = "needs_review"
    INFRA = "infra"


@dataclass
class AgentPostResult:
    """Structured publishing result extracted from the MobileRun agent.

    Field names mirror the real-repo Pydantic ``PostResult`` so an agent's
    ``structured_output`` can be copied attribute-for-attribute via
    ``from_structured``.
    """

    success: bool
    platform: str
    device_serial: str
    account_username: str | None = None
    video_id: str | None = None
    caption: str | None = None
    post_url: str | None = None
    path_used: str | None = None
    failure_reason: str | None = None

    @classmethod
    def from_structured(
        cls,
        obj: Any,
        *,
        device_serial: str,
        video_id: str | None,
        caption: str | None,
    ) -> "AgentPostResult | None":
        if obj is None:
            return None
        return cls(
            success=bool(_attr(obj, "success", False)),
            platform=str(_attr(obj, "platform", "instagram")),
            device_serial=str(_attr(obj, "device_serial", device_serial)),
            account_username=_optional_str(_attr(obj, "account_username", None)),
            video_id=_optional_str(_attr(obj, "video_id", video_id)),
            caption=_optional_str(_attr(obj, "caption", caption)),
            post_url=_optional_str(_attr(obj, "post_url", None)),
            path_used=_optional_str(_attr(obj, "path_used", None)),
            failure_reason=_optional_str(_attr(obj, "failure_reason", None)),
        )


@dataclass
class AgentRunnerResult:
    """Return value of ``MobileRunAgentRunner.run()``.

    Independent of MobileRun so that ``MobileUIAutomationStep`` can map it
    to a ``StepResult`` without importing mobilerun.
    """

    category: ResultCategory
    success: bool
    error_code: str | None
    failure_reason: str | None
    message: str
    structured: AgentPostResult | None = None
    trajectory_paths: list[str] = field(default_factory=list)
    agent_status: str | None = None
    raw_result: Any | None = None


def _attr(obj: Any, name: str, default: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
