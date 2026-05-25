"""Tests for MobileWorker session types."""

from src.worker.session.types import (
    Artifact,
    Mode,
    StepContext,
    StepName,
    StepResult,
    StepStatus,
    Warning,
)


def test_step_result_defaults():
    result = StepResult(step="mobile_ui_automation", status=StepStatus.OK, message="done")
    assert result.code is None
    assert result.retryable is None
    assert result.warnings == []
    assert result.artifacts == []
    assert result.details is None


def test_step_result_failed_with_code():
    result = StepResult(
        step="verification",
        status=StepStatus.FAILED,
        message="caption did not match",
        code="caption_mismatch",
        retryable=False,
    )
    assert result.status == StepStatus.FAILED
    assert result.code == "caption_mismatch"
    assert result.retryable is False


def test_step_context_frozen():
    ctx = StepContext(
        job_id="j1",
        video_id="v1",
        account_id="a1",
        account_environment_id="ae1",
        device_id="d1",
        mode=Mode.MVP,
    )
    assert ctx.job_id == "j1"
    assert ctx.mode == Mode.MVP
    assert ctx.settings == {}


def test_warning_and_artifact():
    w = Warning(code="proxy_deferred", step="environment_apply", detail="proxy not applied")
    assert w.code == "proxy_deferred"

    a = Artifact(artifact_id="art-1", artifact_type="screenshot", label="on_error")
    assert a.artifact_type == "screenshot"


def test_step_name_values():
    assert StepName.MOBILE_UI_AUTOMATION.value == "mobile_ui_automation"
    assert StepName.VERIFICATION.value == "verification"
    assert StepName.CLEANUP.value == "cleanup"
