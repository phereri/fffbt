"""Shared types for the MobileWorker interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class StepName(str, Enum):
    ENVIRONMENT_APPLY = "environment_apply"
    VIDEO_PREPARATION = "video_preparation"
    MOBILE_UI_AUTOMATION = "mobile_ui_automation"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"


class Mode(str, Enum):
    PROOF_OF_POSTING = "proof_of_posting"
    MVP = "mvp"
    PRODUCTION = "production"


@dataclass(frozen=True)
class Warning:
    code: str
    step: str
    detail: str


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    artifact_type: str
    label: str


@dataclass(frozen=True)
class StepContext:
    job_id: str
    video_id: str
    account_id: str
    account_environment_id: str
    device_id: str
    mode: Mode
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    step: str
    status: StepStatus
    message: str
    code: str | None = None
    retryable: bool | None = None
    warnings: list[Warning] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    details: dict[str, Any] | None = None
