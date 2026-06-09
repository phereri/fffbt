"""Tests for the two-level verification step."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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


_SLEEP = "asyncio.sleep"


class TestInputValidation:
    def test_no_device_serial(self):
        step = VerificationStep()
        result = run(step.run(_ctx()))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"
        assert "device_serial" in result.message

    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_serial_from_settings(self, MockWorker, mock_sleep):
        step = VerificationStep()
        ctx = _ctx(settings={"device_serial": "DEV001"})
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


class TestLevel1Failure:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_level1_failure_proceeds_to_level2(
        self, MockWorker, mock_sleep
    ):
        # Level 1 is best-effort: a failed/inconclusive immediate check must NOT
        # short-circuit. The step still waits the delay and runs the
        # authoritative Level 2 dashboard verification (here Level 2 also fails).
        w = _mock_worker()
        w.run_goal.return_value = {"status": "failed"}  # both levels fail
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "verification_failed"
        assert "level 2" in result.message
        mock_sleep.assert_called()  # the pre-dashboard delay happened

    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_level1_exception_proceeds_to_level2(self, MockWorker, mock_sleep):
        # A crash in the Level 1 check is swallowed; the step still proceeds to
        # the delayed Level 2 dashboard verification.
        w = _mock_worker()
        w.run_goal.side_effect = RuntimeError("agent crash")
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert "level 2" in result.message
        mock_sleep.assert_called()


class TestTwoLevelSuccess:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_both_levels_pass_no_url(self, MockWorker, mock_sleep):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # Level 1
            {"status": "success"},  # Level 2
            {"status": "success", "output": {"post_url": ""}},  # URL capture
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert "dashboard" in result.message
        mock_sleep.assert_called_once_with(180)

    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_both_levels_pass_with_url(self, MockWorker, mock_sleep):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # Level 1
            {"status": "completed"},  # Level 2
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


class TestLevel2Failure:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_level1_pass_level2_fail(self, MockWorker, mock_sleep):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # Level 1
            {"status": "failed"},  # Level 2
            {"status": "failed"},  # URL capture
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "verification_failed"
        assert "level 2" in result.message
        mock_sleep.assert_called_once_with(180)


class TestVerificationDelay:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_custom_delay_from_settings(self, MockWorker, mock_sleep):
        w = _mock_worker()
        MockWorker.return_value = w

        step = VerificationStep()
        ctx = _ctx(
            settings={
                "device_serial": "DEV001",
                "verification_delay_seconds": "60",
            }
        )
        result = run(step.run(ctx))
        assert result.status == StepStatus.OK
        mock_sleep.assert_called_once_with(60)

    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_default_delay(self, MockWorker, mock_sleep):
        w = _mock_worker()
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        mock_sleep.assert_called_once_with(180)


class TestUrlCapture:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_url_capture_exception_does_not_fail(self, MockWorker, mock_sleep):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # Level 1
            {"status": "success"},  # Level 2
            RuntimeError("timeout"),  # URL capture throws
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert result.details is None

    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_url_without_instagram_domain_ignored(self, MockWorker, mock_sleep):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # Level 1
            {"status": "success"},  # Level 2
            {
                "status": "success",
                "output": {"post_url": "https://example.com/not-ig"},
            },
        ]
        MockWorker.return_value = w

        step = VerificationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.OK
        assert result.details is None


class TestScreenshots:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_screenshots_taken(self, MockWorker, mock_sleep):
        w = _mock_worker()
        MockWorker.return_value = w

        step = VerificationStep()
        run(step.run(_ctx(), device_serial="DEV001"))

        labels = [call.args[0] for call in w.screenshot.call_args_list]
        assert "level1_verification" in labels
        assert "verification_result" in labels

    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_level1_fail_still_runs_level2_screenshot(self, MockWorker, mock_sleep):
        # Level 1 failure no longer short-circuits, so Level 2 runs and its
        # verification_result screenshot is still taken.
        w = _mock_worker()
        w.run_goal.return_value = {"status": "failed"}
        MockWorker.return_value = w

        step = VerificationStep()
        run(step.run(_ctx(), device_serial="DEV001"))

        labels = [call.args[0] for call in w.screenshot.call_args_list]
        assert "level1_verification" in labels
        assert "verification_result" in labels


class TestStepResultContract:
    @patch(_SLEEP, new_callable=AsyncMock)
    @patch("src.worker.steps.verification.MobilerunWorker")
    def test_result_shape(self, MockWorker, mock_sleep):
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
