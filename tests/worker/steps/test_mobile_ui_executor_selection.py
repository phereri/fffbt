"""Tests for ``MobileUIAutomationStep`` executor selection.

Covers:
- proof_of_posting selects mobilerun_agent by default.
- MOBILE_UI_EXECUTOR=deterministic (env or ctx setting) flips to legacy path.
- unknown executor returns INFRA failure.
- agent path maps the four ``ResultCategory`` outcomes to StepStatus
  correctly and never invokes the deterministic worker path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.worker.agent_runner.result import (
    AgentPostResult,
    AgentRunnerResult,
    ResultCategory,
)
from src.worker.session.types import Mode, StepContext, StepName, StepStatus
from src.worker.steps.mobile_ui_automation import (
    EXECUTOR_DETERMINISTIC,
    EXECUTOR_MOBILERUN_AGENT,
    MobileUIAutomationStep,
    _normalize_hashtags,
)


def _ctx(**overrides) -> StepContext:
    defaults = dict(
        job_id="job-1",
        video_id="vid-1",
        account_id="acct-1",
        account_environment_id="ae-1",
        device_id="dev-1",
        mode=Mode.PROOF_OF_POSTING,
        settings={
            "device_serial": "192.168.5.30:5555",
            "caption_text": "hello world",
        },
    )
    if "settings" in overrides:
        defaults["settings"] = {**defaults["settings"], **overrides.pop("settings")}
    defaults.update(overrides)
    return StepContext(**defaults)


@dataclass
class _StubRunner:
    """Stand-in for ``MobileRunAgentRunner`` so tests never import mobilerun."""

    kwargs: dict
    result: AgentRunnerResult

    async def run(self) -> AgentRunnerResult:
        return self.result


def _stub_factory(captured: dict, result: AgentRunnerResult):
    def factory(**kwargs):
        captured["kwargs"] = kwargs
        captured["call_count"] = captured.get("call_count", 0) + 1
        return _StubRunner(kwargs=kwargs, result=result)

    return factory


def _ok_result() -> AgentRunnerResult:
    return AgentRunnerResult(
        category=ResultCategory.OK,
        success=True,
        error_code=None,
        failure_reason=None,
        message="published",
        structured=AgentPostResult(
            success=True,
            platform="instagram",
            device_serial="192.168.5.30:5555",
        ),
        trajectory_paths=["/tmp/trajectories/run-1.json"],
        agent_status="complete",
    )


def _hard_stop_result(code: str = "logged_out") -> AgentRunnerResult:
    return AgentRunnerResult(
        category=ResultCategory.HARD_STOP,
        success=False,
        error_code=code,
        failure_reason=code,
        message=f"agent reported failure_reason={code!r}",
    )


def _needs_review_result() -> AgentRunnerResult:
    return AgentRunnerResult(
        category=ResultCategory.NEEDS_REVIEW,
        success=False,
        error_code="share_did_not_register",
        failure_reason="share_did_not_register",
        message="agent reported failure_reason='share_did_not_register'",
    )


# ---------------------------------------------------------------------------
# Default + env selection
# ---------------------------------------------------------------------------


class TestExecutorSelection:
    def test_default_selects_mobilerun_agent(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        ctx = _ctx()
        assert MobileUIAutomationStep._select_executor(ctx) == EXECUTOR_MOBILERUN_AGENT

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("MOBILE_UI_EXECUTOR", "deterministic")
        ctx = _ctx()
        assert MobileUIAutomationStep._select_executor(ctx) == EXECUTOR_DETERMINISTIC

    def test_ctx_setting_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("MOBILE_UI_EXECUTOR", "deterministic")
        ctx = _ctx(settings={"mobile_ui_executor": "mobilerun_agent"})
        assert MobileUIAutomationStep._select_executor(ctx) == EXECUTOR_MOBILERUN_AGENT

    def test_unknown_executor_returns_infra_failure(self, monkeypatch):
        monkeypatch.setenv("MOBILE_UI_EXECUTOR", "bogus")
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.FAILED
        assert result.code == "INFRA"
        assert "bogus" in result.message
        # Neither executor was touched.
        assert captured.get("call_count", 0) == 0


# ---------------------------------------------------------------------------
# Agent path — runs the runner, never instantiates MobilerunWorker
# ---------------------------------------------------------------------------


class TestAgentExecutorPath:
    def test_proof_of_posting_uses_agent_runner_by_default(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.OK
        assert captured["call_count"] == 1
        kwargs = captured["kwargs"]
        assert kwargs["device_serial"] == "192.168.5.30:5555"
        assert kwargs["caption"] == "hello world"
        assert kwargs["job_id"] == "job-1"
        assert kwargs["video_id"] == "vid-1"
        assert kwargs["mode"] == "proof_of_posting"

    def test_agent_runner_receives_optional_fields_from_ctx(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        ctx = _ctx(
            settings={
                "hashtags": ["foo", "bar"],
                "expected_username": "acct1",
                "local_video_path": "/tmp/video.mp4",
                "host_video_in_gallery": "video.mp4",
                "mobilerun_overrides": {"max_steps": 30},
                "mobilerun_timeout_seconds": 900,
            }
        )
        asyncio.run(step.run(ctx))
        kw = captured["kwargs"]
        assert kw["hashtags"] == ["foo", "bar"]
        assert kw["expected_username"] == "acct1"
        assert kw["local_video_path"] == "/tmp/video.mp4"
        assert kw["host_video_in_gallery"] == "video.mp4"
        assert kw["model_overrides"] == {"max_steps": 30}
        assert kw["timeout_seconds"] == 900

    def test_success_maps_to_step_ok_with_trajectory_artifacts(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.OK
        assert result.step == StepName.MOBILE_UI_AUTOMATION
        traj_artifacts = [
            a for a in result.artifacts if a.artifact_type == "mobilerun_trajectory"
        ]
        assert len(traj_artifacts) == 1
        assert traj_artifacts[0].artifact_id.endswith("run-1.json")
        assert result.details["mobile_driver"]["executor"] == "mobilerun_agent"
        assert result.details["mobile_driver"]["adb_fallback_used"] is False

    def test_hard_stop_logged_out_maps_to_failed(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _hard_stop_result("logged_out")),
        )
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.FAILED
        assert result.code == "logged_out"
        assert result.retryable is False

    def test_needs_review_share_did_not_register(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _needs_review_result()),
        )
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.NEEDS_REVIEW
        assert result.code == "share_did_not_register"

    def test_agent_runner_construction_failure_returns_infra(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)

        def bad_factory(**kwargs):
            raise RuntimeError("mobilerun not installed")

        step = MobileUIAutomationStep(agent_runner_factory=bad_factory)
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.FAILED
        assert result.code == "INFRA"
        assert "mobilerun not installed" in result.message

    def test_agent_runner_exception_returns_unknown(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)

        class _Boom:
            async def run(self):
                raise RuntimeError("agent kaboom")

        def factory(**kwargs):
            return _Boom()

        step = MobileUIAutomationStep(agent_runner_factory=factory)
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.FAILED
        assert result.code == "UNKNOWN"
        assert "agent kaboom" in result.message

    def test_no_raw_adb_fallback_recorded_in_agent_path(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        result = asyncio.run(step.run(_ctx()))
        # The agent path never constructs a MobilerunWorker, so by
        # construction no ADB fallback can be recorded.
        assert result.details["mobile_driver"]["adb_fallback_used"] is False


class TestHashtagsHandoff:
    """Hashtags must reach the agent as a list[str], never per-character."""

    def test_normalize_hashtags_passthrough_list(self):
        assert _normalize_hashtags(["#football", "#fifa"]) == ["#football", "#fifa"]

    def test_normalize_hashtags_splits_string_into_whole_tags(self):
        # The legacy bug: a joined string was passed to list(), exploding it
        # into single characters. It must split on whitespace/commas instead.
        assert _normalize_hashtags("#football #fifa") == ["#football", "#fifa"]
        assert _normalize_hashtags("#football, #fifa") == ["#football", "#fifa"]

    def test_normalize_hashtags_empty(self):
        assert _normalize_hashtags(None) == []
        assert _normalize_hashtags("") == []
        assert _normalize_hashtags([]) == []

    def test_string_hashtags_setting_reaches_runner_as_list(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        # Simulate a stale/legacy joined-string value in settings.
        ctx = _ctx(settings={"hashtags": "#football #fifa #worldcup"})
        asyncio.run(step.run(ctx))
        assert captured["kwargs"]["hashtags"] == ["#football", "#fifa", "#worldcup"]

    def test_caption_base_preferred_over_full_caption_for_agent(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(captured, _ok_result()),
        )
        ctx = _ctx(
            settings={
                "caption_text": "Body text\n\n#football #fifa",
                "caption_base": "Body text",
                "hashtags": ["#football", "#fifa"],
            }
        )
        asyncio.run(step.run(ctx))
        # Agent receives the body only; the goal builder appends the tags once.
        assert captured["kwargs"]["caption"] == "Body text"
        assert captured["kwargs"]["hashtags"] == ["#football", "#fifa"]


# ---------------------------------------------------------------------------
# Deterministic path opt-in
# ---------------------------------------------------------------------------


class TestDeterministicExecutorPath:
    def test_explicit_deterministic_calls_legacy_executor(self, monkeypatch):
        monkeypatch.setenv("MOBILE_UI_EXECUTOR", "deterministic")
        # Patch _run_deterministic_executor on the instance so we don't need
        # to spin up a fake MobilerunWorker; we only care that the dispatcher
        # picked the right branch.
        captured: dict = {}

        async def fake_det(ctx, serial, caption):
            captured["called"] = (serial, caption)
            from src.worker.session.types import StepResult

            return StepResult(
                step=StepName.MOBILE_UI_AUTOMATION,
                status=StepStatus.OK,
                message="det-ok",
            )

        agent_captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(agent_captured, _ok_result()),
        )
        step._run_deterministic_executor = fake_det  # type: ignore[assignment]
        result = asyncio.run(step.run(_ctx()))
        assert result.status is StepStatus.OK
        assert captured.get("called") == ("192.168.5.30:5555", "hello world")
        # Agent path must NOT have been invoked.
        assert agent_captured.get("call_count", 0) == 0

    def test_ctx_setting_deterministic_overrides_default(self, monkeypatch):
        monkeypatch.delenv("MOBILE_UI_EXECUTOR", raising=False)
        captured: dict = {}

        async def fake_det(ctx, serial, caption):
            captured["called"] = True
            from src.worker.session.types import StepResult

            return StepResult(
                step=StepName.MOBILE_UI_AUTOMATION,
                status=StepStatus.OK,
                message="det-ok",
            )

        agent_captured: dict = {}
        step = MobileUIAutomationStep(
            agent_runner_factory=_stub_factory(agent_captured, _ok_result()),
        )
        step._run_deterministic_executor = fake_det  # type: ignore[assignment]
        ctx = _ctx(settings={"mobile_ui_executor": "deterministic"})
        result = asyncio.run(step.run(ctx))
        assert result.status is StepStatus.OK
        assert captured["called"] is True
        assert agent_captured.get("call_count", 0) == 0
