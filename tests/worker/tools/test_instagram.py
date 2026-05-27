"""Tests for Instagram-specific tools."""

import asyncio
from unittest.mock import AsyncMock, patch

from src.worker.tools.instagram import (
    hide_ime,
    paste_text,
    tap_by_resource_id,
    tap_by_text,
    verify_caption_text,
)


# ---------------------------------------------------------------------------
# verify_caption_text (sync, no mocks needed)
# ---------------------------------------------------------------------------


class TestVerifyCaptionText:
    def test_exact_match(self):
        nodes = [
            {
                "text": "Goal by Messi #football",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,0][100,100]",
            }
        ]
        result = verify_caption_text("Goal by Messi #football", ui_nodes=nodes)
        assert result.success
        assert "exactly" in result.message

    def test_whitespace_tolerant(self):
        nodes = [
            {
                "text": "Goal  by  Messi  #football",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,0][100,100]",
            }
        ]
        result = verify_caption_text("Goal by Messi #football", ui_nodes=nodes)
        assert result.success
        assert "whitespace-tolerant" in result.message

    def test_mismatch(self):
        nodes = [
            {
                "text": "Wrong caption",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,0][100,100]",
            }
        ]
        result = verify_caption_text("Goal by Messi #football", ui_nodes=nodes)
        assert not result.success
        assert "mismatch" in result.message

    def test_placeholder_detected(self):
        nodes = [
            {
                "text": "Write a caption or add a hashtag…",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,0][100,100]",
            }
        ]
        result = verify_caption_text("Goal", ui_nodes=nodes)
        assert not result.success
        assert "placeholder" in result.message

    def test_no_caption_field(self):
        result = verify_caption_text(
            "Goal", ui_nodes=[{"text": "Share", "resourceId": "other"}]
        )
        assert not result.success
        assert "not found" in result.message

    def test_prefers_non_placeholder(self):
        nodes = [
            {
                "text": "Write a caption or add a hashtag…",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,0][100,100]",
            },
            {
                "text": "My real caption",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,0][100,100]",
            },
        ]
        result = verify_caption_text("My real caption", ui_nodes=nodes)
        assert result.success


# ---------------------------------------------------------------------------
# tap_by_resource_id
# ---------------------------------------------------------------------------


class TestTapByResourceId:
    def test_empty_ui(self):
        result = asyncio.run(
            tap_by_resource_id("DEV001", "share_button", ui_nodes=[])
        )
        assert not result.success
        assert "empty UI tree" in result.message

    def test_no_match(self):
        nodes = [{"text": "x", "resourceId": "other", "bounds": "[0,0][10,10]"}]
        result = asyncio.run(
            tap_by_resource_id("DEV001", "share_button", ui_nodes=nodes)
        )
        assert not result.success

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_successful_tap(self, mock_tap):
        nodes = [
            {
                "text": "",
                "resourceId": "com.instagram.android:id/share_button",
                "className": "android.widget.FrameLayout",
                "bounds": "[0,100][200,300]",
            }
        ]
        result = asyncio.run(
            tap_by_resource_id("DEV001", "share_button", ui_nodes=nodes)
        )
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 100, 200)

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_share_button_picks_lowest(self, mock_tap):
        nodes = [
            {
                "text": "",
                "resourceId": "com.instagram.android:id/share_button",
                "className": "android.widget.FrameLayout",
                "bounds": "[0,100][200,200]",
            },
            {
                "text": "",
                "resourceId": "com.instagram.android:id/share_button",
                "className": "android.widget.FrameLayout",
                "bounds": "[0,1600][200,1700]",
            },
        ]
        result = asyncio.run(
            tap_by_resource_id("DEV001", "share_button", ui_nodes=nodes)
        )
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 100, 1650)

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_caption_picks_largest_autocomplete(self, mock_tap):
        nodes = [
            {
                "text": "Prompt",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.TextView",
                "bounds": "[0,400][100,420]",
            },
            {
                "text": "Write a caption...",
                "resourceId": "com.instagram.android:id/caption_input_text_view",
                "className": "android.widget.AutoCompleteTextView",
                "bounds": "[0,400][1080,800]",
            },
        ]
        result = asyncio.run(
            tap_by_resource_id(
                "DEV001", "caption_input_text_view", ui_nodes=nodes
            )
        )
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 540, 600)

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_full_resource_id_match(self, mock_tap):
        nodes = [
            {
                "text": "",
                "resourceId": "com.instagram.android:id/next_button_textview",
                "className": "android.widget.TextView",
                "bounds": "[800,50][1080,100]",
            }
        ]
        result = asyncio.run(
            tap_by_resource_id(
                "DEV001",
                "com.instagram.android:id/next_button_textview",
                ui_nodes=nodes,
            )
        )
        assert result.success

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_contains_text_filter(self, mock_tap):
        nodes = [
            {
                "text": "Cancel",
                "resourceId": "com.instagram.android:id/button",
                "bounds": "[0,0][100,50]",
            },
            {
                "text": "OK",
                "resourceId": "com.instagram.android:id/button",
                "bounds": "[200,0][300,50]",
            },
        ]
        result = asyncio.run(
            tap_by_resource_id(
                "DEV001", "button", ui_nodes=nodes, contains_text="OK"
            )
        )
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 250, 25)


# ---------------------------------------------------------------------------
# tap_by_text
# ---------------------------------------------------------------------------


class TestTapByText:
    def test_empty_text(self):
        result = asyncio.run(tap_by_text("DEV001", "", ui_nodes=[]))
        assert not result.success

    def test_no_match(self):
        nodes = [{"text": "Cancel", "bounds": "[0,0][100,50]"}]
        result = asyncio.run(tap_by_text("DEV001", "Share", ui_nodes=nodes))
        assert not result.success

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_smallest_match(self, mock_tap):
        nodes = [
            {"text": "Next", "bounds": "[0,0][200,100]", "resourceId": ""},
            {"text": "Next step", "bounds": "[0,0][400,200]", "resourceId": ""},
        ]
        result = asyncio.run(tap_by_text("DEV001", "Next", ui_nodes=nodes))
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 100, 50)

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_largest_match(self, mock_tap):
        nodes = [
            {"text": "Write a caption hint", "bounds": "[0,0][100,50]", "resourceId": ""},
            {"text": "Write a caption box", "bounds": "[0,0][1080,400]", "resourceId": ""},
        ]
        result = asyncio.run(
            tap_by_text(
                "DEV001", "Write a caption", ui_nodes=nodes, prefer="largest"
            )
        )
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 540, 200)

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_exclude_text(self, mock_tap):
        nodes = [
            {"text": "Prompt", "bounds": "[0,0][100,50]", "resourceId": ""},
            {
                "text": "Write a caption or add a hashtag",
                "bounds": "[0,0][400,200]",
                "resourceId": "",
            },
        ]
        result = asyncio.run(
            tap_by_text(
                "DEV001",
                "caption",
                ui_nodes=nodes,
                exclude_text_exact=("Prompt",),
            )
        )
        assert result.success

    @patch("src.worker.tools.instagram.input_tap", new_callable=AsyncMock)
    def test_exact_match(self, mock_tap):
        nodes = [
            {"text": "Done", "bounds": "[0,0][100,50]", "resourceId": ""},
            {"text": "Done editing", "bounds": "[0,0][200,50]", "resourceId": ""},
        ]
        result = asyncio.run(
            tap_by_text("DEV001", "Done", ui_nodes=nodes, exact=True)
        )
        assert result.success
        mock_tap.assert_called_once_with("DEV001", 50, 25)


# ---------------------------------------------------------------------------
# hide_ime
# ---------------------------------------------------------------------------


class TestHideIme:
    @patch(
        "src.worker.tools.instagram.ime_input_shown",
        new_callable=AsyncMock,
        return_value=False,
    )
    def test_already_hidden(self, mock_shown):
        result = asyncio.run(hide_ime("DEV001"))
        assert result.success
        assert "already hidden" in result.message

    @patch("src.worker.tools.instagram.shell", new_callable=AsyncMock)
    @patch("src.worker.tools.instagram.ime_input_shown", new_callable=AsyncMock)
    def test_hidden_after_back(self, mock_shown, mock_shell):
        mock_shown.side_effect = [True, False]
        result = asyncio.run(hide_ime("DEV001"))
        assert result.success
        mock_shell.assert_called_once_with("DEV001", "input keyevent 4", timeout=10)

    @patch("src.worker.tools.instagram.shell", new_callable=AsyncMock)
    @patch("src.worker.tools.instagram.ime_input_shown", new_callable=AsyncMock)
    def test_keyevent_failure(self, mock_shown, mock_shell):
        mock_shown.return_value = True
        mock_shell.side_effect = RuntimeError("device offline")
        result = asyncio.run(hide_ime("DEV001"))
        assert not result.success
        assert "keyevent BACK failed" in result.message


# ---------------------------------------------------------------------------
# paste_text
# ---------------------------------------------------------------------------


class TestPasteText:
    @patch("src.worker.tools.instagram._adb_keyboard_restore", new_callable=AsyncMock)
    @patch(
        "src.worker.tools.instagram._keyboard_ensure_active",
        new_callable=AsyncMock,
        return_value=(None, None),
    )
    @patch("src.worker.tools.instagram.shell", new_callable=AsyncMock)
    def test_empty_text(self, mock_shell, mock_ensure, mock_restore):
        result = asyncio.run(paste_text("DEV001", "", ui_nodes=[]))
        assert result.success
        assert "(empty)" in result.message

    @patch("src.worker.tools.instagram._adb_keyboard_restore", new_callable=AsyncMock)
    @patch(
        "src.worker.tools.instagram._keyboard_ensure_active",
        new_callable=AsyncMock,
        return_value=(None, "adbkeyboard"),
    )
    @patch("src.worker.tools.instagram.shell", new_callable=AsyncMock)
    def test_successful_paste_legacy_adb_keyboard(self, mock_shell, mock_ensure, mock_restore):
        mock_shell.return_value = "Broadcasting: ...\nBroadcast completed: result=0"
        result = asyncio.run(
            paste_text("DEV001", "Hello world", ui_nodes=[])
        )
        assert result.success
        assert "ADB_INPUT_B64" in result.message

    @patch("src.worker.tools.instagram._adb_keyboard_restore", new_callable=AsyncMock)
    @patch("src.worker.tools.instagram._mobilerun_keyboard_input", new_callable=AsyncMock)
    @patch(
        "src.worker.tools.instagram._keyboard_ensure_active",
        new_callable=AsyncMock,
        return_value=(None, "mobilerun"),
    )
    @patch("src.worker.tools.instagram.shell", new_callable=AsyncMock)
    def test_successful_paste_mobilerun_keyboard(
        self, mock_shell, mock_ensure, mock_mobilerun_input, mock_restore
    ):
        mock_mobilerun_input.return_value = True
        result = asyncio.run(paste_text("DEV001", "Hello world", ui_nodes=[]))
        assert result.success
        assert "MobileRun keyboard" in result.message
        mock_mobilerun_input.assert_called_once()
        mock_shell.assert_not_called()

    @patch("src.worker.tools.instagram._adb_keyboard_restore", new_callable=AsyncMock)
    @patch(
        "src.worker.tools.instagram._keyboard_ensure_active",
        new_callable=AsyncMock,
        return_value=("com.android.inputmethod.latin/.LatinIME", "mobilerun"),
    )
    @patch(
        "src.worker.tools.instagram._mobilerun_keyboard_input",
        new_callable=AsyncMock,
        return_value=True,
    )
    @patch("src.worker.tools.instagram.shell", new_callable=AsyncMock)
    def test_restores_ime(self, mock_shell, mock_mobilerun_input, mock_ensure, mock_restore):
        asyncio.run(paste_text("DEV001", "text", ui_nodes=[]))
        mock_restore.assert_called_once_with("DEV001", "com.android.inputmethod.latin/.LatinIME")
