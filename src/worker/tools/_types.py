"""Shared types for worker tools."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolResult:
    """Structured return value for all worker tools."""

    success: bool
    message: str

    @staticmethod
    def ok(message: str) -> ToolResult:
        return ToolResult(success=True, message=message)

    @staticmethod
    def fail(message: str) -> ToolResult:
        return ToolResult(success=False, message=f"Failed: {message}")
