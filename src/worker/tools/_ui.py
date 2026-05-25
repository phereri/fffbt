"""UI tree parsing helpers for Android accessibility nodes.

Works with flat node dicts as produced by Mobilerun's UI state or
parsed from ``uiautomator dump`` XML. Each node is a dict with keys
like ``text``, ``resourceId``, ``className``, ``bounds``.
"""

from __future__ import annotations

from typing import Any


def node_text(node: dict[str, Any]) -> str:
    return str(
        node.get("text")
        or node.get("contentDescription")
        or node.get("content_description")
        or ""
    )


def node_resource_id(node: dict[str, Any]) -> str:
    return str(node.get("resourceId") or node.get("resource_id") or "")


def parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    """Parse bounds from string or list. Returns (x1, y1, x2, y2) or None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return int(value[0]), int(value[1]), int(value[2]), int(value[3])
        except (TypeError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.startswith("[") and "]" in s:
            cleaned = s.replace("[", "").replace("]", ",").rstrip(",")
            parts = cleaned.split(",")
        else:
            parts = s.split(",")
        nums = [int(p.strip()) for p in parts[:4]]
        if len(nums) == 4:
            return nums[0], nums[1], nums[2], nums[3]
    except (TypeError, ValueError):
        return None
    return None


def normalize_caption_text(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for ch in ("—", "–", "−", "­"):
        t = t.replace(ch, "-")
    lines = [line.rstrip() for line in t.split("\n")]
    return "\n".join(lines).strip()


def is_instagram_caption_placeholder(text: str) -> bool:
    t = normalize_caption_text(text).lower().replace("…", ".").replace("...", ".")
    return "write a caption" in t and "hashtag" in t


def walk_plain_ui(value: Any) -> list[dict[str, Any]]:
    """Flatten a nested dict/list UI tree into a list of node dicts."""
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for child in value.values():
            out.extend(walk_plain_ui(child))
    elif isinstance(value, list):
        for item in value:
            out.extend(walk_plain_ui(item))
    return out
