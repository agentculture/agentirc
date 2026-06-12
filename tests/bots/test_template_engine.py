"""Tests for the template engine module."""

import pytest

from agentirc.bots.template_engine import render_fallback, render_template


class TestRenderTemplate:
    """Tests for render_template function."""

    @pytest.mark.parametrize(
        "template,payload,expected",
        [
            # Normal substitution: simple key
            ("{nick}", {"nick": "alice"}, "alice"),
            # Normal substitution: nested key with dot-path
            ("{event.nick}", {"event": {"nick": "bob"}}, "bob"),
            # Missing key fallback: returns None when token unresolved
            ("{missing_key}", {"nick": "charlie"}, None),
            # Missing nested key: returns None when path incomplete
            ("{event.nick}", {"event": {}}, None),
            # JSON/dict value rendering: dict becomes str representation
            ("{data}", {"data": {"x": 1, "y": 2}}, "{'x': 1, 'y': 2}"),
            # JSON/dict value rendering: list becomes str representation
            ("{items}", {"items": [1, 2, 3]}, "[1, 2, 3]"),
            # Multiple tokens in one template
            ("{nick}: {msg}", {"nick": "dave", "msg": "hello"}, "dave: hello"),
            # Payload as string: wrapped under "body" key
            ("{body}", "test_string", "test_string"),
            # Nested payload as dict with "body" alias
            ("{body.nick}", {"nick": "eve"}, "eve"),
            # None value becomes "null" string
            ("{nullable}", {"nullable": None}, "null"),
        ],
    )
    def test_render_template_cases(self, template, payload, expected):
        """Test render_template with various inputs."""
        result = render_template(template, payload)
        assert result == expected


class TestRenderFallback:
    """Tests for render_fallback function."""

    @pytest.mark.parametrize(
        "payload,mode,expected_substring",
        [
            # JSON mode (default): compact JSON output
            ({"nick": "frank", "msg": "hi"}, "json", '"nick": "frank"'),
            # JSON mode handles non-ASCII
            ({"text": "你好"}, "json", "你好"),
            # String mode: uses str() representation
            ({"key": "value"}, "str", "{'key': 'value'}"),
            # Empty dict in JSON mode
            ({}, "json", "{}"),
        ],
    )
    def test_render_fallback_cases(self, payload, mode, expected_substring):
        """Test render_fallback with various modes."""
        result = render_fallback(payload, mode=mode)
        assert expected_substring in result


class TestRenderTemplateWithFallback:
    """Integration test: render_template + render_fallback together."""

    def test_fallback_when_template_fails(self):
        """When render_template returns None, use render_fallback."""
        template = "{missing_key}"
        payload = {"nick": "grace"}

        # Template render fails (missing key)
        rendered_template = render_template(template, payload)
        assert rendered_template is None

        # Fall back to JSON rendering
        fallback_result = render_fallback(payload, mode="json")
        assert '"nick": "grace"' in fallback_result

    def test_successful_template_render(self):
        """When template succeeds, fallback is not needed."""
        template = "User {nick} says: {message}"
        payload = {"nick": "henry", "message": "hello world"}

        rendered_template = render_template(template, payload)
        assert rendered_template == "User henry says: hello world"
