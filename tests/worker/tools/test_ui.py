"""Tests for UI tree parsing helpers."""

from src.worker.tools._ui import (
    is_instagram_caption_placeholder,
    node_resource_id,
    node_text,
    normalize_caption_text,
    parse_bounds,
    walk_plain_ui,
)


class TestParseBounds:
    def test_bracket_format(self):
        assert parse_bounds("[0,100][200,300]") == (0, 100, 200, 300)

    def test_comma_format(self):
        assert parse_bounds("0,100,200,300") == (0, 100, 200, 300)

    def test_list_format(self):
        assert parse_bounds([0, 100, 200, 300]) == (0, 100, 200, 300)

    def test_tuple_format(self):
        assert parse_bounds((10, 20, 30, 40)) == (10, 20, 30, 40)

    def test_none(self):
        assert parse_bounds(None) is None

    def test_empty_string(self):
        assert parse_bounds("") is None

    def test_invalid_string(self):
        assert parse_bounds("not-bounds") is None

    def test_short_list(self):
        assert parse_bounds([1, 2]) is None


class TestNodeAccessors:
    def test_node_text_from_text(self):
        assert node_text({"text": "Share"}) == "Share"

    def test_node_text_from_content_description(self):
        assert node_text({"contentDescription": "Back"}) == "Back"

    def test_node_text_from_snake_case(self):
        assert node_text({"content_description": "OK"}) == "OK"

    def test_node_text_empty(self):
        assert node_text({}) == ""

    def test_node_text_prefers_text(self):
        assert node_text({"text": "A", "contentDescription": "B"}) == "A"

    def test_node_resource_id_camel(self):
        assert (
            node_resource_id({"resourceId": "com.ig:id/share_button"})
            == "com.ig:id/share_button"
        )

    def test_node_resource_id_snake(self):
        assert node_resource_id({"resource_id": "share_button"}) == "share_button"

    def test_node_resource_id_empty(self):
        assert node_resource_id({}) == ""


class TestNormalizeCaptionText:
    def test_basic(self):
        assert normalize_caption_text("Hello World") == "Hello World"

    def test_em_dash(self):
        assert normalize_caption_text("a—b") == "a-b"

    def test_en_dash(self):
        assert normalize_caption_text("a–b") == "a-b"

    def test_trailing_whitespace(self):
        assert normalize_caption_text("  hello  \n  world  \n") == "hello\n  world"

    def test_crlf(self):
        assert normalize_caption_text("a\r\nb") == "a\nb"

    def test_empty(self):
        assert normalize_caption_text("") == ""


class TestIsInstagramCaptionPlaceholder:
    def test_placeholder(self):
        assert is_instagram_caption_placeholder(
            "Write a caption or add a hashtag…"
        )

    def test_placeholder_dots(self):
        assert is_instagram_caption_placeholder(
            "Write a caption or add a hashtag..."
        )

    def test_real_caption(self):
        assert not is_instagram_caption_placeholder("Goal by Messi #football")

    def test_empty(self):
        assert not is_instagram_caption_placeholder("")


class TestWalkPlainUi:
    def test_flat_list(self):
        nodes = [{"text": "A"}, {"text": "B"}]
        assert walk_plain_ui(nodes) == [{"text": "A"}, {"text": "B"}]

    def test_nested_dict(self):
        tree = {"text": "root", "children": [{"text": "child"}]}
        result = walk_plain_ui(tree)
        assert len(result) >= 2
        texts = [n.get("text") for n in result if "text" in n]
        assert "root" in texts

    def test_empty(self):
        assert walk_plain_ui([]) == []
        assert walk_plain_ui({}) == [{}]
