"""Ported Mobilerun custom tools for the FFFBT worker layer.

Each tool retains the original algorithm but uses FFFBT interfaces:
device_serial instead of ActionContext, ToolResult instead of (bool, str)
tuples, and no Mobilerun-internal or legacy poker_videos dependencies.
"""

from src.worker.tools._types import ToolResult
from src.worker.tools.device import (
    device_summary,
    mock_location_status,
    set_mock_location_app,
)
from src.worker.tools.instagram import (
    hide_ime,
    paste_text,
    tap_by_resource_id,
    tap_by_text,
    tap_share_and_confirm,
    verify_caption_text,
)
from src.worker.tools.video import prepare_video_for_android, push_video_to_gallery

__all__ = [
    "ToolResult",
    "prepare_video_for_android",
    "push_video_to_gallery",
    "hide_ime",
    "paste_text",
    "tap_by_resource_id",
    "tap_by_text",
    "tap_share_and_confirm",
    "verify_caption_text",
    "device_summary",
    "mock_location_status",
    "set_mock_location_app",
]
