"""Tests for the mobile_ui_automation step."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from src.worker.session.types import Mode, StepContext, StepName, StepStatus
from src.worker.steps.mobile_ui_automation import (
    MobileUIAutomationStep,
    _detect_hard_stop,
    _parse_page_source,
    _parse_xml_ui,
)
from src.worker.tools._types import ToolResult


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


# ---------------------------------------------------------------------------
# _parse_xml_ui
# ---------------------------------------------------------------------------


class TestParseXmlUi:
    def test_valid_xml(self):
        xml = (
            '<hierarchy>'
            '<node text="Share" resource-id="com.instagram.android:id/share_button" '
            'class="android.widget.FrameLayout" bounds="[0,1600][1080,1700]" />'
            '</hierarchy>'
        )
        nodes = _parse_xml_ui(xml)
        assert len(nodes) >= 1
        share = [n for n in nodes if n.get("resourceId", "").endswith("share_button")]
        assert len(share) == 1
        assert share[0]["className"] == "android.widget.FrameLayout"
        assert share[0]["bounds"] == "[0,1600][1080,1700]"

    def test_invalid_xml(self):
        assert _parse_xml_ui("not xml at all") == []

    def test_empty(self):
        assert _parse_xml_ui("") == []


class TestParsePageSource:
    def test_xml(self):
        xml = '<hierarchy><node text="Profile" bounds="[0,0][100,50]" /></hierarchy>'
        nodes = _parse_page_source(xml)
        assert any(n.get("text") == "Profile" for n in nodes)

    def test_json_flat(self):
        data = [{"text": "Profile", "resourceId": "profile_tab", "bounds": "[0,0][100,50]"}]
        nodes = _parse_page_source(json.dumps(data))
        assert any(n.get("text") == "Profile" for n in nodes)

    def test_empty_string(self):
        assert _parse_page_source("") == []
        assert _parse_page_source("   ") == []

    def test_invalid_json(self):
        assert _parse_page_source("{bad json") == []


# ---------------------------------------------------------------------------
# _detect_hard_stop
# ---------------------------------------------------------------------------


class TestDetectHardStop:
    def test_action_blocked(self):
        nodes = [{"text": "Action blocked. We restrict certain activity."}]
        result = _detect_hard_stop(nodes)
        assert result is not None
        assert result[0] == "action_blocked"

    def test_logged_out(self):
        nodes = [{"text": "Log in to Instagram"}]
        result = _detect_hard_stop(nodes)
        assert result is not None
        assert result[0] == "logged_out"

    def test_no_stop(self):
        nodes = [{"text": "Professional dashboard"}, {"text": "Trial Reels"}]
        assert _detect_hard_stop(nodes) is None

    def test_empty_nodes(self):
        assert _detect_hard_stop([]) is None

    def test_content_description(self):
        nodes = [{"contentDescription": "Try again later"}]
        result = _detect_hard_stop(nodes)
        assert result is not None
        assert result[0] == "action_blocked"


# ---------------------------------------------------------------------------
# MobileUIAutomationStep
# ---------------------------------------------------------------------------


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
        step = MobileUIAutomationStep()
        result = run(step.run(_ctx()))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"
        assert "device_serial" in result.message

    def test_no_caption(self):
        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001"))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"
        assert "caption" in result.message

    def test_serial_from_settings(self):
        step = MobileUIAutomationStep()
        ctx = _ctx(settings={"device_serial": "DEV001"})
        result = run(step.run(ctx))
        assert result.status == StepStatus.FAILED
        assert "caption" in result.message


class TestConnectFailure:
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_genfarmer_unreachable(self, MockWorker):
        MockWorker.return_value.connect.side_effect = ConnectionError("refused")
        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.FAILED
        assert result.code == "INFRA"
        assert "connect failed" in result.message.lower()


class TestHardStopOnLaunch:
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_action_blocked_after_open(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # open_instagram
        ]
        w.page_source.return_value = json.dumps(
            [{"text": "Action blocked. We restrict certain activity to protect our community."}]
        )
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.FAILED
        assert result.code == "action_blocked"

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_logged_out_on_launch(self, MockWorker):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "failed", "error": "app not loaded"}
        w.page_source.return_value = json.dumps(
            [{"text": "Log in to Instagram"}, {"text": "Create new account"}]
        )
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.FAILED
        assert result.code == "logged_out"


class TestNavigationFailure:
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_trial_reels_unavailable(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # open
            {"status": "failed", "error": "Professional dashboard tile not found"},
        ]
        w.page_source.return_value = json.dumps([{"text": "Settings"}])
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.FAILED
        assert result.code == "trial_reels_unavailable"

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_unknown_screen(self, MockWorker):
        w = _mock_worker()
        w.run_goal.side_effect = [
            {"status": "success"},  # open
            {"status": "failed", "error": "cannot find element"},
        ]
        w.page_source.return_value = json.dumps([{"text": "Some random screen"}])
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "unknown_screen"


class TestCaptionFlow:
    @patch("src.worker.steps.mobile_ui_automation.paste_text")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_caption_paste_failure(self, MockWorker, mock_paste):
        w = _mock_worker()
        # Successful navigation
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([
            {
                "text": "Write a caption",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,400][1080,800]",
            },
        ])
        MockWorker.return_value = w

        mock_paste.return_value = ToolResult.fail("caption field not found")

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="My caption"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "unknown_screen"
        assert "caption paste" in result.message

    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch("src.worker.steps.mobile_ui_automation.paste_text")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_caption_mismatch(self, MockWorker, mock_paste, mock_verify):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([
            {
                "text": "Wrong caption text",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,400][1080,800]",
            },
        ])
        MockWorker.return_value = w

        mock_paste.return_value = ToolResult.ok("pasted 10 chars")
        mock_verify.return_value = ToolResult.fail(
            "caption verification mismatch: expected='My caption'; observed='Wrong caption text'"
        )

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="My caption"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "caption_mismatch"


class TestShareFlow:
    @patch("src.worker.steps.mobile_ui_automation.tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch("src.worker.steps.mobile_ui_automation.paste_text")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_share_did_not_register(self, MockWorker, mock_paste, mock_verify, mock_share):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([{"text": "Share screen"}])
        MockWorker.return_value = w

        mock_paste.return_value = ToolResult.ok("pasted")
        mock_verify.return_value = ToolResult.ok("caption verified")
        mock_share.return_value = ToolResult.fail("share did not register")

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "share_did_not_register"


class TestHappyPath:
    @patch("src.worker.steps.mobile_ui_automation.tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch("src.worker.steps.mobile_ui_automation.paste_text")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_full_success(self, MockWorker, mock_paste, mock_verify, mock_share):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([
            {
                "text": "My caption #reels",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,400][1080,800]",
            },
            {
                "text": "Share",
                "resourceId": "com.instagram.android:id/share_button",
                "className": "android.widget.FrameLayout",
                "bounds": "[0,1600][1080,1700]",
            },
        ])
        MockWorker.return_value = w

        mock_paste.return_value = ToolResult.ok("pasted 17 chars via ADB_INPUT_B64")
        mock_verify.return_value = ToolResult.ok("caption verified exactly (17 chars)")
        mock_share.return_value = ToolResult.ok(
            "share confirmed: activity changed in 2.3s"
        )

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="My caption #reels"))
        assert result.status == StepStatus.OK
        assert result.step == StepName.MOBILE_UI_AUTOMATION
        assert "published" in result.message.lower()

        w.connect.assert_called_once()
        w.disconnect.assert_called_once()
        assert w.screenshot.call_count >= 1


class TestStepResultContract:
    @patch("src.worker.steps.mobile_ui_automation.tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch("src.worker.steps.mobile_ui_automation.paste_text")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_success_shape(self, MockWorker, mock_paste, mock_verify, mock_share):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([{"text": "ok"}])
        MockWorker.return_value = w
        mock_paste.return_value = ToolResult.ok("ok")
        mock_verify.return_value = ToolResult.ok("ok")
        mock_share.return_value = ToolResult.ok("ok")

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="D", caption_text="C"))

        assert result.step == StepName.MOBILE_UI_AUTOMATION
        assert result.status in StepStatus
        assert isinstance(result.message, str)

    def test_failure_shape(self):
        step = MobileUIAutomationStep()
        result = run(step.run(_ctx()))

        assert result.step == StepName.MOBILE_UI_AUTOMATION
        assert result.status == StepStatus.FAILED
        assert result.code is not None
        assert isinstance(result.message, str)
