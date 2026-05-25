"""Tests for the verification step."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from src.worker.session.types import Mode, StepContext, StepName, StepStatus
from src.worker.steps.verification import VerificationStep


def _ctx(**overrides) -> StepContext:
    defaults = dict(
        job_id="j1",
        video_id="v1",
        account_id="a1",
        account_environment_id="ae1",
        device_id="d1",
        mode=Mode.MVP,
        settings={},
    )
    defaults.update(overrides)
    return StepContext(**defaults)


def run(coro):
    return asyncio.run(coro)


def _mock_worker():
    w = MagicMock()
    w.connect = MagicMock()
    w.disconnect = MagicMock()
    w.screenshot = MagicMock(return_value=b"png")
    w.page_source = MagicMock(return_value="")
    w.run_goal = MagicMock(return_value={"status": "success"})
    return w


class TestInputValidation:
    def test_no_device_serial(self):
        step = VerificationStep()
        result = run(step.run(_ctx()))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"
        assert "device_serial" in result.message

    def test_serial_from_settings(self):
        step = VerificationStep()
        ctx = _ctx(settings={"device_serial": "DEV001"})
        with patch("src.worker.steps.verification.MobilerunWorker") as MockWorker:
            w = _mock_worker()
            MockWorker.return_value = w
            result = run(step.run(ctx))
        assert result.status in (StepStatus.OK, StepStatus.NEEDS_REVIEW)


class TestConnectFailure:
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_genfarmer_unreachable(self, MockWorker):
        MockWorker.return_value.connect.side_effect = ConnectionError("refused")
        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"


class TestVerificationSuccess:
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_post_verified_no_url(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # verify post visible
            {"status": "success", "output": {"post_url": ""}},  # capture URL
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert "verified" in result.message

    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_post_verified_with_url(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},
            {
                "status": "success",
                "output": {"post_url": "https://www.instagram.com/reel/ABC123/"},
            },
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert "instagram.com" in result.message
        assert result.details is not None
        assert result.details["post_url"] == "https://www.instagram.com/reel/ABC123/"


class TestVerificationFailed:
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_post_not_visible(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "failed"},  # verify post visible
            {"status": "failed"},  # capture URL
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "verification_failed"


class TestUrlCapture:
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_url_capture_exception_does_not_fail(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # verify
            RuntimeError("timeout"),  # capture URL throws
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert result.details is None

    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_url_without_instagram_domain_ignored(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},
            {"status": "success", "output": {"post_url": "https://example.com/not-ig"}},
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert result.details is None


class TestStepResultContract:
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_result_shape(self, MockWorker):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))

        assert result.step == StepName.VERIFICATION
        assert result.status in StepStatus
        assert isinstance(result.message, str)

    def test_failure_shape(self):
        step = VerificationStep()
        result = run(step.run(_ctx()))

        assert result.step == StepName.VERIFICATION
        assert result.status == StepStatus.FAILED
        assert result.code is not None
