"""Tests for ``MobileRunAgentRunner`` and the failure-reason mapping."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.worker.agent_runner.goal import HARD_STOP_RULES, build_trial_reel_goal
from src.worker.agent_runner.mobilerun_agent_runner import (
    AgentFactoryRequest,
    MobileRunAgentRunner,
    map_failure_reason,
)
from src.worker.agent_runner.result import (
    AgentPostResult,
    AgentRunnerResult,
    ResultCategory,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeStructured:
    success: bool
    platform: str = "instagram"
    device_serial: str = ""
    account_username: str | None = None
    video_id: str | None = None
    caption: str | None = None
    post_url: str | None = None
    failure_reason: str | None = None


@dataclass
class _FakeResultEvent:
    structured_output: _FakeStructured | None = None
    status: str | None = "complete"
    reason: str | None = None


class _FakeAgent:
    def __init__(self, result_event: _FakeResultEvent | Exception):
        self._result = result_event
        self.run_calls = 0

    async def run(self) -> Any:
        self.run_calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _runner(**overrides) -> MobileRunAgentRunner:
    defaults = dict(
        device_serial="192.168.5.30:5555",
        job_id="job-abc",
        caption="hello world",
        hashtags=["foo", "bar"],
        expected_username="acct1",
        video_id="vid-1",
        local_video_path="/tmp/video.mp4",
        host_video_in_gallery="video.mp4",
        timeout_seconds=900,
    )
    defaults.update(overrides)
    return MobileRunAgentRunner(**defaults)


def _make_factory(agent: _FakeAgent, captured: dict) -> Any:
    def factory(request: AgentFactoryRequest):
        captured["request"] = request
        return agent
    return factory


# ---------------------------------------------------------------------------
# build_trial_reel_goal
# ---------------------------------------------------------------------------


class TestBuildGoal:
    def test_contains_required_fields(self):
        goal = build_trial_reel_goal(
            device_serial="192.168.1.10:5555",
            caption="caption body",
            hashtags=["#foo", "bar"],
            expected_username="acct1",
            video_id="vid-1",
            host_video_in_gallery="video.mp4",
        )
        assert "192.168.1.10:5555" in goal
        assert "caption body" in goal
        assert "#foo" in goal and "#bar" in goal
        assert "acct1" in goal
        assert "vid-1" in goal
        assert "Trial Reel" in goal
        assert "Mobilerun TCP" in goal
        assert "video.mp4" in goal
        assert "logged_out" in goal
        assert "action_blocked" in goal
        assert "account_suspended" in goal
        assert "trial_reels_unavailable" in goal
        assert "share_did_not_register" in goal
        assert "caption_mismatch" in goal
        assert HARD_STOP_RULES.split("\n", 1)[0] in goal

    def test_local_video_branch_when_no_gallery(self):
        goal = build_trial_reel_goal(
            device_serial="serial",
            caption="x",
            hashtags=None,
            expected_username=None,
            video_id=None,
            local_video_path="/path/video.mp4",
        )
        assert "prepare_video_for_android" in goal
        assert "push_video_to_gallery" in goal
        assert "/path/video.mp4" in goal

    def test_hashtags_rendered_intact_and_once(self):
        # Body caption (no tags) + a hashtags list → tags appended exactly once,
        # as whole tags (regression guard against per-character explosion).
        goal = build_trial_reel_goal(
            device_serial="serial",
            caption="Match day energy",
            hashtags=["#football", "fifa"],
            expected_username=None,
            video_id=None,
            host_video_in_gallery="x.mp4",
        )
        assert "#football" in goal
        assert "#fifa" in goal
        # Each tag appears exactly once in the rendered caption block.
        assert goal.count("#football") == 1
        assert goal.count("#fifa") == 1
        # No per-character artifacts like "#f #o #o ...".
        assert "#f #o" not in goal

    def test_skip_prep_when_host_pushed(self):
        goal = build_trial_reel_goal(
            device_serial="serial",
            caption="x",
            hashtags=None,
            expected_username=None,
            video_id=None,
            host_video_in_gallery="x.mp4",
        )
        assert "skipped — host already pushed" in goal
        assert "Do NOT call ``prepare_video_for_android``" in goal
        assert "x.mp4" in goal
        # The prepare/push instruction from the local-video branch must NOT
        # appear — the host already pushed the file.
        assert "Run ``prepare_video_for_android`` with source_path" not in goal
        assert "Run ``push_video_to_gallery`` with that same prepared path" not in goal


# ---------------------------------------------------------------------------
# map_failure_reason
# ---------------------------------------------------------------------------


class TestFailureMapping:
    @pytest.mark.parametrize(
        "reason,expected_code,expected_category",
        [
            ("logged_out", "logged_out", ResultCategory.HARD_STOP),
            ("LOGGED_OUT", "logged_out", ResultCategory.HARD_STOP),
            ("action_blocked", "action_blocked", ResultCategory.HARD_STOP),
            ("Action blocked", "action_blocked", ResultCategory.HARD_STOP),
            ("account_suspended", "account_suspended", ResultCategory.HARD_STOP),
            ("login_challenge", "login_challenge", ResultCategory.HARD_STOP),
            ("two-factor", "login_challenge", ResultCategory.HARD_STOP),
            ("2fa", "login_challenge", ResultCategory.HARD_STOP),
            ("checkpoint", "login_challenge", ResultCategory.HARD_STOP),
            ("trial_reels_unavailable", "trial_reels_unavailable", ResultCategory.HARD_STOP),
            ("share_did_not_register", "share_did_not_register", ResultCategory.NEEDS_REVIEW),
            ("final_ok_did_not_register", "final_ok_did_not_register", ResultCategory.NEEDS_REVIEW),
            ("caption_mismatch", "caption_mismatch", ResultCategory.NEEDS_REVIEW),
            ("caption_no_match", "caption_mismatch", ResultCategory.NEEDS_REVIEW),
        ],
    )
    def test_known_reasons(self, reason, expected_code, expected_category):
        code, category = map_failure_reason(reason)
        assert code == expected_code
        assert category is expected_category

    def test_unknown_reason_maps_to_needs_review_unknown_screen(self):
        code, category = map_failure_reason("zorglub")
        assert code == "unknown_screen"
        assert category is ResultCategory.NEEDS_REVIEW

    def test_none_reason_maps_to_unknown_screen(self):
        code, category = map_failure_reason(None)
        assert code == "unknown_screen"
        assert category is ResultCategory.NEEDS_REVIEW

    def test_substring_match(self):
        code, category = map_failure_reason("something about logged_out happened")
        assert code == "logged_out"
        assert category is ResultCategory.HARD_STOP


# ---------------------------------------------------------------------------
# MobileRunAgentRunner.build_request
# ---------------------------------------------------------------------------


class TestBuildRequest:
    def test_request_carries_inputs_and_tcp_override(self):
        runner = _runner(model_overrides={"max_steps": 30})
        request = runner.build_request()
        assert request.device_serial == "192.168.5.30:5555"
        assert request.platform == "instagram"
        assert request.timeout_seconds == 900
        assert request.overrides["use_tcp"] is True
        assert request.overrides["max_steps"] == 30
        assert request.variables["device_serial"] == "192.168.5.30:5555"
        assert request.variables["job_id"] == "job-abc"
        assert request.variables["video_id"] == "vid-1"
        assert request.variables["caption"] == "hello world"
        assert request.variables["hashtags"] == ["foo", "bar"]
        assert request.variables["expected_username"] == "acct1"
        assert request.variables["host_video_in_gallery"] == "video.mp4"
        assert request.config_path.endswith("config/mobilerun/config.yaml")
        assert "Trial Reel" in request.goal

    def test_config_path_env_override(self, monkeypatch):
        monkeypatch.setenv("MOBILERUN_CONFIG", "/custom/path.yaml")
        runner = _runner()
        assert runner.build_request().config_path == "/custom/path.yaml"

    def test_explicit_config_path_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("MOBILERUN_CONFIG", "/env/path.yaml")
        runner = _runner(config_path="/explicit/path.yaml")
        assert runner.build_request().config_path == "/explicit/path.yaml"

    def test_rejects_non_proof_of_posting_mode(self):
        with pytest.raises(ValueError):
            _runner(mode="production")

    def test_rejects_missing_device_serial(self):
        with pytest.raises(ValueError):
            _runner(device_serial="")

    def test_rejects_missing_caption(self):
        with pytest.raises(ValueError):
            _runner(caption="")


# ---------------------------------------------------------------------------
# MobileRunAgentRunner.run — happy path + failure mapping
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_success_maps_to_category_ok(self):
        agent = _FakeAgent(
            _FakeResultEvent(
                structured_output=_FakeStructured(
                    success=True,
                    device_serial="192.168.5.30:5555",
                    account_username="acct1",
                    video_id="vid-1",
                    caption="hello world",
                    post_url=None,
                )
            )
        )
        captured: dict = {}
        runner = _runner(agent_factory=_make_factory(agent, captured))
        result = asyncio.run(runner.run())

        assert agent.run_calls == 1
        assert result.category is ResultCategory.OK
        assert result.success is True
        assert result.error_code is None
        assert result.failure_reason is None
        assert isinstance(result.structured, AgentPostResult)
        assert result.structured.success is True
        # The factory was called with the runner's built request.
        req: AgentFactoryRequest = captured["request"]
        assert req.device_serial == "192.168.5.30:5555"
        assert "Trial Reel" in req.goal

    def test_failure_with_known_reason_maps_to_hard_stop(self):
        agent = _FakeAgent(
            _FakeResultEvent(
                structured_output=_FakeStructured(
                    success=False,
                    device_serial="192.168.5.30:5555",
                    failure_reason="logged_out",
                )
            )
        )
        runner = _runner(agent_factory=_make_factory(agent, {}))
        result = asyncio.run(runner.run())
        assert result.category is ResultCategory.HARD_STOP
        assert result.success is False
        assert result.error_code == "logged_out"
        assert result.failure_reason == "logged_out"

    def test_failure_unknown_reason_maps_to_needs_review(self):
        agent = _FakeAgent(
            _FakeResultEvent(
                structured_output=_FakeStructured(
                    success=False,
                    device_serial="192.168.5.30:5555",
                    failure_reason="zorglub",
                )
            )
        )
        runner = _runner(agent_factory=_make_factory(agent, {}))
        result = asyncio.run(runner.run())
        assert result.category is ResultCategory.NEEDS_REVIEW
        assert result.error_code == "unknown_screen"
        assert result.failure_reason == "zorglub"

    def test_failure_without_structured_falls_back_to_top_level_reason(self):
        agent = _FakeAgent(
            _FakeResultEvent(
                structured_output=None,
                reason="action_blocked",
                status="error",
            )
        )
        runner = _runner(agent_factory=_make_factory(agent, {}))
        result = asyncio.run(runner.run())
        assert result.category is ResultCategory.HARD_STOP
        assert result.error_code == "action_blocked"
        assert result.failure_reason == "action_blocked"
        assert result.structured is None
        assert result.agent_status == "error"

    def test_agent_exception_maps_to_infra(self):
        agent = _FakeAgent(RuntimeError("network exploded"))
        runner = _runner(agent_factory=_make_factory(agent, {}))
        result = asyncio.run(runner.run())
        assert result.category is ResultCategory.INFRA
        assert result.error_code == "UNKNOWN"
        assert "network exploded" in result.failure_reason

    def test_trajectory_paths_captured(self, tmp_path: Path):
        traj = tmp_path / "trajectories"
        traj.mkdir()
        agent = _FakeAgent(
            _FakeResultEvent(
                structured_output=_FakeStructured(
                    success=True, device_serial="192.168.5.30:5555"
                )
            )
        )
        captured_paths: list[str] = []

        def factory(request: AgentFactoryRequest):
            # Simulate the agent writing a trajectory file mid-run.
            new = traj / "run-1.json"
            new.write_text("{}")
            captured_paths.append(str(new))
            return agent

        runner = _runner(
            trajectories_dir=str(traj),
            agent_factory=factory,
        )
        result = asyncio.run(runner.run())
        assert result.success is True
        assert any(p.endswith("run-1.json") for p in result.trajectory_paths)
