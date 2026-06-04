"""Tests for the mobile_ui_automation step.

The default ``MOBILE_UI_EXECUTOR`` is ``mobilerun_agent``. This file exercises
the legacy deterministic executor, so every ``_ctx()`` here opts back into it
via ``settings["mobile_ui_executor"] = "deterministic"``. The agent-path
dispatch tests live in ``test_mobile_ui_executor_selection.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.worker.session.types import Artifact, Mode, StepContext, StepName, StepStatus
from src.worker.steps.mobile_ui_automation import (
    MobileUIAutomationStep,
    _bottom_right_next,
    _detect_hard_stop,
    _parse_activity_dump,
    _parse_page_source,
    _text_center,
    _parse_xml_ui,
)
from src.worker.tools._types import ToolResult


@pytest.fixture(autouse=True)
def _force_deterministic_executor(monkeypatch):
    """Pin every test in this file to the legacy deterministic executor."""
    monkeypatch.setenv("MOBILE_UI_EXECUTOR", "deterministic")


def _ctx(**overrides) -> StepContext:
    defaults = dict(
        job_id="j1",
        video_id="v1",
        account_id="a1",
        account_environment_id="ae1",
        device_id="d1",
        mode=Mode.MVP,
        settings={"mobile_ui_executor": "deterministic"},
    )
    if "settings" in overrides:
        defaults["settings"] = {**defaults["settings"], **overrides.pop("settings")}
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


class TestParseActivityDump:
    def test_instagram_resource_nodes(self):
        dump = (
            "com.instagram.common.ui.base.IgButton{5ce5136 "
            "VFED..... 589,87-800,203 #7f0b0c02 app:id/clips_next_button}\n"
            "com.instagram.common.ui.base.IgTextView{abc "
            "V.ED..... 0,0-0,0 #7f0b0000 app:id/hidden}"
        )
        nodes = _parse_activity_dump(dump)
        assert len(nodes) == 1
        assert nodes[0]["resourceId"].endswith("clips_next_button")
        assert nodes[0]["bounds"] == "[589,87][800,203]"


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


class TestEditorNext:
    def test_bottom_right_next_ignores_top_disabled_next(self):
        nodes = [
            {
                "text": "Next",
                "bounds": "[589,87][800,203]",
                "isEnabled": False,
            },
            {
                "text": "Next ->",
                "boundsInScreen": {
                    "left": 824,
                    "top": 1566,
                    "right": 1014,
                    "bottom": 1676,
                },
                "isEnabled": True,
            },
        ]

        assert _bottom_right_next(nodes) == (919, 1621)

    def test_text_center_finds_trial_reels_tile(self):
        nodes = [
            {"text": "Partnership ads", "bounds": "[0,100][600,220]"},
            {"text": "Trial reels", "bounds": "[20,820][460,910]", "isEnabled": True},
        ]

        assert _text_center(nodes, "trial reels") == (240, 865)


# ---------------------------------------------------------------------------
# MobileUIAutomationStep
# ---------------------------------------------------------------------------


def _mock_worker():
    w = MagicMock()
    w.connect = MagicMock()
    w.disconnect = MagicMock()
    w.open_app = MagicMock(return_value={"status": "success"})
    w.screenshot = MagicMock(return_value=b"png")
    w.page_source = MagicMock(return_value="")
    w.activity_page_source = MagicMock(return_value="")
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


class TestDriverSelection:
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_proof_of_posting_worker_forces_tcp_without_adb_ui_fallback(self, MockWorker):
        MockWorker.return_value.connect.side_effect = ConnectionError("stop after construct")

        step = MobileUIAutomationStep()
        run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))

        MockWorker.assert_called_once_with(
            device_serial="DEV001",
            genfarmer_url="http://127.0.0.1:55554",
            adb_fallback=False,
            use_tcp=True,
        )

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_mobile_driver_actions_are_reported_in_step_details(self, MockWorker):
        w = _mock_worker()
        w.actions_log = [
            {
                "action": "tap",
                "details": {
                    "driver": "mobilerun_tcp",
                    "fallback_used": False,
                    "x": 1,
                    "y": 2,
                },
            },
            {
                "action": "type_text",
                "details": {
                    "driver": "mobilerun_tcp",
                    "fallback_used": False,
                    "length": 3,
                },
            },
        ]
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        with patch.object(step, "_type_caption", return_value=ToolResult.ok("typed")):
            with patch.object(step, "_tap_share_and_confirm", return_value=ToolResult.ok("shared")):
                with patch(
                    "src.worker.steps.mobile_ui_automation.verify_caption_text",
                    return_value=ToolResult.ok("verified"),
                ):
                    result = run(
                        step.run(_ctx(), device_serial="DEV001", caption_text="cap")
                    )

        driver = result.details["mobile_driver"]
        assert driver["primary"] == "mobilerun_tcp"
        assert driver["use_tcp"] is True
        assert driver["adb_fallback_used"] is False
        assert driver["actions"] == w.actions_log


class TestHardStopOnLaunch:
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_action_blocked_after_open(self, MockWorker):
        w = _mock_worker()
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
        w.open_app.return_value = {"status": "failed", "error": "app not loaded"}
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
        w.run_goal.return_value = {
            "status": "failed",
            "error": "Professional dashboard tile not found",
        }
        w.page_source.return_value = json.dumps([{"text": "Settings"}])
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.FAILED
        assert result.code == "trial_reels_unavailable"

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_unknown_screen(self, MockWorker):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "failed", "error": "cannot find element"}
        w.page_source.return_value = json.dumps([{"text": "Some random screen"}])
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "unknown_screen"

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_share_screen_not_reached_uses_specific_code(self, MockWorker):
        w = _mock_worker()
        w.run_goal.return_value = {
            "status": "failed",
            "error_code": "share_screen_not_reached",
            "error": "Share screen not reached after editor Next fallback",
        }
        w.page_source.return_value = json.dumps([{"text": "Next"}])
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "share_screen_not_reached"

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_trial_gallery_not_reached_uses_specific_code(self, MockWorker):
        w = _mock_worker()
        w.run_goal.return_value = {
            "status": "failed",
            "error_code": "trial_reels_gallery_not_reached",
            "error": "Trial Reels gallery not detected after create",
        }
        w.page_source.return_value = json.dumps([{"text": "Partnership ads"}])
        MockWorker.return_value = w

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "trial_reels_gallery_not_reached"


class TestCaptionFlow:
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_caption_paste_failure(self, MockWorker, mock_type_caption):
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

        mock_type_caption.return_value = ToolResult.fail("caption field not found")

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="My caption"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "unknown_screen"
        assert "caption paste" in result.message

    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_caption_mismatch(self, MockWorker, mock_type_caption, mock_verify):
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

        mock_type_caption.return_value = ToolResult.ok("typed 10 chars via MobileRun TCP")
        mock_verify.return_value = ToolResult.fail(
            "caption verification mismatch: expected='My caption'; observed='Wrong caption text'"
        )

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="My caption"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "caption_mismatch"


class TestShareFlow:
    @patch.object(MobileUIAutomationStep, "_tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_share_did_not_register(self, MockWorker, mock_type_caption, mock_verify, mock_share):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([{"text": "Share screen"}])
        MockWorker.return_value = w

        mock_type_caption.return_value = ToolResult.ok("typed via MobileRun TCP")
        mock_verify.return_value = ToolResult.ok("caption verified")
        mock_share.return_value = ToolResult.fail("share did not register")

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))
        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "share_did_not_register"

    def test_dismiss_caption_keyboard_taps_top_right_ok_before_share(self):
        step = MobileUIAutomationStep()
        worker = _mock_worker()
        worker.taps = []
        worker.tap = lambda x, y: worker.taps.append((x, y))
        focused = json.dumps(
            [
                {
                    "text": "OK",
                    "resourceId": "com.instagram.android:id/action_bar_button_text",
                    "boundsInScreen": {
                        "left": 939,
                        "top": 83,
                        "right": 1059,
                        "bottom": 210,
                    },
                    "isEnabled": True,
                },
                {
                    "text": "caption",
                    "resourceId": "com.instagram.android:id/caption_input_text_view",
                    "bounds": "[42,496][1038,711]",
                    "isFocused": True,
                },
                {
                    "resourceId": "com.instagram.android:id/caption_add_on_recyclerview",
                    "bounds": "[0,711][1080,862]",
                },
            ]
        )
        dismissed = json.dumps(
            [
                {
                    "resourceId": "com.instagram.android:id/share_button",
                    "contentDescription": "Share",
                    "bounds": "[561,1625][1038,1741]",
                }
            ]
        )
        worker.page_source.side_effect = [focused, dismissed]

        run(step._dismiss_caption_keyboard(worker))

        assert worker.taps == [(999, 146)]

    def test_share_taps_ok_then_share_when_caption_keyboard_open(self):
        step = MobileUIAutomationStep()
        worker = _mock_worker()
        worker.taps = []
        worker.tap = lambda x, y: worker.taps.append((x, y))
        focused = json.dumps(
            [
                {
                    "text": "OK",
                    "resourceId": "com.instagram.android:id/action_bar_button_text",
                    "boundsInScreen": {
                        "left": 939,
                        "top": 83,
                        "right": 1059,
                        "bottom": 210,
                    },
                },
                {
                    "resourceId": "com.instagram.android:id/caption_input_text_view",
                    "bounds": "[42,496][1038,711]",
                    "isFocused": True,
                },
            ]
        )
        share = json.dumps(
            [
                {
                    "resourceId": "com.instagram.android:id/share_button",
                    "contentDescription": "Share",
                    "bounds": "[561,1625][1038,1741]",
                }
            ]
        )
        posted = json.dumps([{"text": "Home"}])
        # Legacy path adds one upfront read for new-reel-screen detection.
        worker.page_source.side_effect = [focused, focused, share, share, posted]

        result = run(step._tap_share_and_confirm(worker))

        assert result.success
        assert worker.taps == [(999, 146), (799, 1683)]


class TestHappyPath:
    @patch.object(MobileUIAutomationStep, "_tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_full_success(self, MockWorker, mock_type_caption, mock_verify, mock_share):
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

        mock_type_caption.return_value = ToolResult.ok("typed 17 chars via MobileRun TCP")
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
    @patch.object(MobileUIAutomationStep, "_tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_success_shape(self, MockWorker, mock_type_caption, mock_verify, mock_share):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([{"text": "ok"}])
        MockWorker.return_value = w
        mock_type_caption.return_value = ToolResult.ok("ok")
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


# ---------------------------------------------------------------------------
# Final OK / New reel publish handling
# ---------------------------------------------------------------------------


_TRIAL_BANNER_TEXT = (
    "This is a trial reel and will only be shown to non-followers at first."
)


def _new_reel_node(text: str, bounds: str) -> dict:
    return {"text": text, "bounds": bounds}


def _final_new_reel_screen_with_ok(*, focused: bool = False) -> str:
    caption_node: dict = {
        "text": "match preview tonight",
        "resourceId": "com.instagram.android:id/caption_input_text_view",
        "className": "android.widget.AutoCompleteTextView",
        "bounds": "[42,496][1038,711]",
    }
    if focused:
        caption_node["isFocused"] = True
    nodes = [
        _new_reel_node("New reel", "[120,80][420,170]"),
        _new_reel_node(_TRIAL_BANNER_TEXT, "[42,260][1038,400]"),
        caption_node,
        {
            "text": "OK",
            "resourceId": "com.instagram.android:id/action_bar_button_text",
            "bounds": "[939,83][1059,210]",
            "isEnabled": True,
        },
        # An invisible root node big enough to anchor screen size at 1080x1920.
        {"bounds": "[0,0][1080,1920]"},
    ]
    if focused:
        nodes.append(
            {
                "resourceId": "com.instagram.android:id/caption_add_on_recyclerview",
                "bounds": "[0,711][1080,862]",
            }
        )
    return json.dumps(nodes)


def _final_new_reel_screen_without_ok() -> str:
    nodes = [
        _new_reel_node("New reel", "[120,80][420,170]"),
        _new_reel_node(_TRIAL_BANNER_TEXT, "[42,260][1038,400]"),
        {
            "text": "match preview tonight",
            "resourceId": "com.instagram.android:id/caption_input_text_view",
            "className": "android.widget.AutoCompleteTextView",
            "bounds": "[42,496][1038,711]",
        },
        # Root node to anchor screen size.
        {"bounds": "[0,0][1080,1920]"},
    ]
    return json.dumps(nodes)


def _post_publish_screen() -> str:
    return json.dumps([{"text": "For you"}, {"text": "Home"}])


class TestFinalOkDetection:
    def test_on_new_reel_screen_true_with_title_caption_and_banner(self):
        step = MobileUIAutomationStep()
        ui = _parse_page_source(_final_new_reel_screen_with_ok())
        assert step._on_new_reel_screen(ui) is True

    def test_on_new_reel_screen_false_for_legacy_share_screen(self):
        step = MobileUIAutomationStep()
        ui = _parse_page_source(
            json.dumps(
                [
                    {
                        "resourceId": "com.instagram.android:id/caption_input_text_view",
                        "bounds": "[42,496][1038,711]",
                    },
                    {
                        "resourceId": "com.instagram.android:id/share_button",
                        "bounds": "[561,1625][1038,1741]",
                    },
                ]
            )
        )
        assert step._on_new_reel_screen(ui) is False

    def test_final_ok_center_uses_accessible_node_when_exposed(self):
        step = MobileUIAutomationStep()
        ui = _parse_page_source(_final_new_reel_screen_with_ok())
        assert step._final_ok_center(ui) == (999, 146)

    def test_final_ok_center_none_when_no_top_right_ok(self):
        step = MobileUIAutomationStep()
        ui = _parse_page_source(_final_new_reel_screen_without_ok())
        assert step._final_ok_center(ui) is None

    def test_top_right_fallback_coords_uses_inferred_screen_size(self):
        step = MobileUIAutomationStep()
        ui = _parse_page_source(_final_new_reel_screen_without_ok())
        coords = step._top_right_fallback_coords(ui)
        assert coords is not None
        x, y = coords
        # x ~ width * 0.925, y ~ height * 0.07 on a 1080x1920 device.
        assert 0.88 * 1080 <= x <= 0.97 * 1080
        assert 0.04 * 1920 <= y <= 0.10 * 1920


def _stable_worker():
    """MobilerunWorker mock without an auto-mocked hide_ime attribute."""
    w = _mock_worker()
    w.taps = []
    w.tap = lambda x, y: w.taps.append((x, y))
    w.hide_ime = None  # disable the hide_ime branch in _dismiss_keyboard_safely
    return w


class TestFinalOkTapsPublish:
    def test_caption_focused_keyboard_open_publishes_via_ok(self):
        """New reel screen with keyboard + OK visible -> safe dismiss + tap OK."""
        step = MobileUIAutomationStep()
        worker = _stable_worker()
        focused = _final_new_reel_screen_with_ok(focused=True)
        cleared = _final_new_reel_screen_with_ok(focused=False)
        # Reads: outer detection, dismiss banner-branch read, dismiss after-tap
        # read, outer re-read after dismiss, _tap_final_ok poll.
        worker.page_source.side_effect = [
            focused,
            focused,
            cleared,
            cleared,
            _post_publish_screen(),
        ]

        result = run(step._tap_share_and_confirm(worker))

        assert result.success, result.message
        # OK must be tapped at the accessible-node coords.
        assert (999, 146) in worker.taps
        # OK is the last tap; banner dismiss (if any) precedes it.
        assert worker.taps[-1] == (999, 146)

    def test_ok_exposed_in_tree_uses_accessible_node(self):
        step = MobileUIAutomationStep()
        worker = _stable_worker()
        stable = _final_new_reel_screen_with_ok(focused=False)
        worker.page_source.side_effect = [stable, _post_publish_screen()]

        result = run(step._tap_share_and_confirm(worker))

        assert result.success, result.message
        assert worker.taps == [(999, 146)]
        assert "accessible_node" in result.message

    def test_ok_not_exposed_uses_top_right_fallback(self):
        step = MobileUIAutomationStep()
        worker = _stable_worker()
        no_ok = _final_new_reel_screen_without_ok()
        worker.page_source.side_effect = [no_ok, _post_publish_screen()]

        result = run(step._tap_share_and_confirm(worker))

        assert result.success, result.message
        assert "top_right_fallback" in result.message
        x, y = worker.taps[0]
        assert 0.88 * 1080 <= x <= 0.97 * 1080
        assert 0.04 * 1920 <= y <= 0.10 * 1920

    def test_no_raw_adb_tap_for_final_ok(self):
        """The publish OK must go through worker.tap (MobileRun TCP)."""
        step = MobileUIAutomationStep()
        worker = _stable_worker()
        stable = _final_new_reel_screen_with_ok(focused=False)
        worker.page_source.side_effect = [stable, _post_publish_screen()]

        with patch("src.worker.tools._adb.shell") as adb_shell:
            run(step._tap_share_and_confirm(worker))

        adb_shell.assert_not_called()
        assert worker.taps == [(999, 146)]


class TestFinalOkDidNotRegister:
    def test_returns_final_ok_did_not_register_when_screen_unchanged(self):
        step = MobileUIAutomationStep()
        worker = _stable_worker()
        # Use a stable (no-keyboard) screen so we only tap OK and poll.
        stable = _final_new_reel_screen_with_ok(focused=False)
        # First call: detection. Subsequent calls: polling (always stable).
        worker.page_source.side_effect = [stable] * 50

        async def _fast_sleep(_s: float) -> None:
            return None

        with patch("asyncio.sleep", new=_fast_sleep):
            result = run(step._tap_share_and_confirm(worker))

        assert not result.success
        assert "final_ok_did_not_register" in result.message
        assert worker.taps == [(999, 146)]

    @patch.object(MobileUIAutomationStep, "_tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_execute_emits_final_ok_did_not_register_not_share_code(
        self, MockWorker, mock_type_caption, mock_verify, mock_share
    ):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([{"text": "Share screen"}])
        MockWorker.return_value = w
        mock_type_caption.return_value = ToolResult.ok("typed")
        mock_verify.return_value = ToolResult.ok("verified")
        mock_share.return_value = ToolResult.fail(
            "final_ok_did_not_register: New reel screen still present after OK tap"
        )

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))

        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "final_ok_did_not_register"
        assert result.code != "share_did_not_register"

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_logged_out_hard_stop_persists_screenshot_and_ui_dump(
        self, MockWorker, tmp_path
    ):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        ui_source = json.dumps([{"text": "Log in to Instagram"}])
        w = _mock_worker()
        w.open_app.return_value = {"status": "failed", "error": "app not loaded"}
        # detection read + capture-helper read (after screenshot)
        w.page_source.side_effect = [ui_source, ui_source]
        w.screenshot.return_value = png_bytes
        MockWorker.return_value = w

        step = MobileUIAutomationStep(artifacts_dir=str(tmp_path))
        result = run(
            step.run(_ctx(), device_serial="DEV001", caption_text="cap")
        )

        assert result.status == StepStatus.FAILED
        assert result.code == "logged_out"

        artifacts_dir = tmp_path / "mobile_ui" / "j1"
        pngs = list(artifacts_dir.glob("hard_stop_logged_out_*.png"))
        ui_dumps = list(artifacts_dir.glob("hard_stop_logged_out_*.ui.json"))
        assert len(pngs) == 1 and pngs[0].read_bytes() == png_bytes
        assert len(ui_dumps) == 1
        assert "Log in to Instagram" in ui_dumps[0].read_text()

        kinds = {a.artifact_type for a in result.artifacts}
        labels = {a.label for a in result.artifacts}
        assert "screenshot" in kinds and "ui_dump" in kinds
        assert {"hard_stop_logged_out"} == labels

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_action_blocked_hard_stop_persists_artifacts(
        self, MockWorker, tmp_path
    ):
        png_bytes = b"\x89PNGblocked"
        ui_source = json.dumps(
            [{"text": "Action blocked. We restrict certain activity."}]
        )
        w = _mock_worker()
        w.page_source.side_effect = [ui_source, ui_source]
        w.screenshot.return_value = png_bytes
        MockWorker.return_value = w

        step = MobileUIAutomationStep(artifacts_dir=str(tmp_path))
        result = run(
            step.run(_ctx(), device_serial="DEV001", caption_text="cap")
        )

        assert result.code == "action_blocked"
        artifacts_dir = tmp_path / "mobile_ui" / "j1"
        assert list(artifacts_dir.glob("hard_stop_action_blocked_*.png"))
        assert list(artifacts_dir.glob("hard_stop_action_blocked_*.ui.json"))
        assert any(
            a.artifact_type == "screenshot" for a in result.artifacts
        )

    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_hard_stop_artifact_capture_failure_does_not_break_result(
        self, MockWorker, tmp_path
    ):
        ui_source = json.dumps([{"text": "Log in to Instagram"}])
        w = _mock_worker()
        w.open_app.return_value = {"status": "failed"}
        w.page_source.side_effect = [ui_source, ui_source]
        # screenshot raises -> helper swallows; result must still be the
        # hard-stop fail.
        w.screenshot.side_effect = RuntimeError("driver disconnected")
        MockWorker.return_value = w

        step = MobileUIAutomationStep(artifacts_dir=str(tmp_path))
        result = run(
            step.run(_ctx(), device_serial="DEV001", caption_text="cap")
        )

        assert result.status == StepStatus.FAILED
        assert result.code == "logged_out"
        # UI dump still written even when the screenshot failed.
        artifacts_dir = tmp_path / "mobile_ui" / "j1"
        assert list(artifacts_dir.glob("hard_stop_logged_out_*.ui.json"))
        assert not list(artifacts_dir.glob("hard_stop_logged_out_*.png"))
        assert any(a.artifact_type == "ui_dump" for a in result.artifacts)
        assert not any(a.artifact_type == "screenshot" for a in result.artifacts)

    @patch.object(MobileUIAutomationStep, "_tap_share_and_confirm")
    @patch("src.worker.steps.mobile_ui_automation.verify_caption_text")
    @patch.object(MobileUIAutomationStep, "_type_caption")
    @patch("src.worker.steps.mobile_ui_automation.MobilerunWorker")
    def test_execute_still_returns_share_code_for_legacy_share_button_fail(
        self, MockWorker, mock_type_caption, mock_verify, mock_share
    ):
        w = _mock_worker()
        w.run_goal.return_value = {"status": "success"}
        w.page_source.return_value = json.dumps([{"text": "Share screen"}])
        MockWorker.return_value = w
        mock_type_caption.return_value = ToolResult.ok("typed")
        mock_verify.return_value = ToolResult.ok("verified")
        # Legacy fail does not start with final_ok_did_not_register.
        mock_share.return_value = ToolResult.fail(
            "share did not register before timeout"
        )

        step = MobileUIAutomationStep()
        result = run(step.run(_ctx(), device_serial="DEV001", caption_text="cap"))

        assert result.status == StepStatus.NEEDS_REVIEW
        assert result.code == "share_did_not_register"
