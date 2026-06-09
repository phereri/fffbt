"""Tests for the MobileRun custom-tool wiring (Instagram Trial-Reel helpers)."""

from __future__ import annotations

import json

import pytest

from src.worker.agent_runner.custom_tools import (
    _parse_portal_state,
    build_instagram_custom_tools,
)

_EXPECTED_TOOLS = {
    "hide_ime",
    "tap_share_and_confirm",
    "verify_caption_text",
    "tap_by_resource_id",
    "tap_by_text",
}


class TestBuildInstagramCustomTools:
    def test_registers_expected_tools(self):
        tools = build_instagram_custom_tools(
            serial="DEV001", video_id="vid", caption="hello #x"
        )
        assert _EXPECTED_TOOLS <= set(tools)
        # paste_text must NOT be registered — it regressed caption entry; the
        # agent uses stock ``type`` instead.
        assert "paste_text" not in tools

    def test_each_tool_has_callable_function_and_metadata(self):
        tools = build_instagram_custom_tools(serial="DEV001")
        for name, spec in tools.items():
            assert callable(spec["function"]), f"{name}: function not callable"
            assert isinstance(spec["parameters"], dict), f"{name}: bad parameters"
            assert spec["description"], f"{name}: empty description"

    def test_adapters_are_async_for_registry_await(self):
        import inspect

        tools = build_instagram_custom_tools(serial="DEV001")
        for name, spec in tools.items():
            assert inspect.iscoroutinefunction(spec["function"]), name


class TestParsePortalState:
    def test_parses_a11y_tree_into_flat_nodes(self):
        inner = json.dumps(
            {
                "a11y_tree": [
                    {
                        "index": 1,
                        "text": "banner",
                        "bounds": "0, 0, 1080, 200",
                        "children": [
                            {"index": 2, "text": "child", "bounds": "0, 0, 10, 10"}
                        ],
                    }
                ]
            }
        )
        raw = "Row: 0 result=" + json.dumps({"status": "success", "result": inner})
        nodes = _parse_portal_state(raw)
        texts = {n.get("text") for n in nodes if isinstance(n, dict)}
        assert "banner" in texts
        assert "child" in texts  # nested children are flattened

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "Row: 0 result=not-json",
            'Row: 0 result={"status":"error","error":"x"}',
            "no result marker here",
        ],
    )
    def test_malformed_input_returns_empty(self, raw):
        assert _parse_portal_state(raw) == []
