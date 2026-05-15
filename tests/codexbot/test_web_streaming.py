"""Tests for the live pane streaming extractor."""

from codexbot.web.streaming import _extract_stream_body


def test_extract_drops_chrome_and_status() -> None:
    pane = (
        "Some context\n"
        "\n"
        "> what's the weather in Tokyo\n"
        "\n"
        "Looking up Tokyo weather…\n"
        "It's 18C and partly cloudy.\n"
        "\n"
        "·  Working… (esc to interrupt)\n"
        "────────────────────────────────────────────\n"
        "  ❯\n"
        "────────────────────────────────────────────\n"
        "  [Opus] Context: 34%\n"
        "  ⏵⏵ bypass permissions\n"
    )
    body = _extract_stream_body(pane)
    assert "Looking up Tokyo weather" in body
    assert "18C and partly cloudy" in body
    assert "esc to interrupt" not in body  # spinner line stripped
    assert "what's the weather" not in body  # user echo stripped
    assert "Context: 34%" not in body  # chrome stripped


def test_extract_returns_empty_when_no_body() -> None:
    pane = (
        "> hi\n"
        "\n"
        "·  Working… (esc to interrupt)\n"
        "────────────────────────────────────────────\n"
        "  ❯\n"
    )
    assert _extract_stream_body(pane) == ""


def test_extract_strips_ansi() -> None:
    pane = "> ask\n\n\x1b[31mhello\x1b[0m there"
    body = _extract_stream_body(pane)
    assert body == "hello there"


def test_extract_no_user_echo_keeps_whole_body() -> None:
    pane = "First assistant line\nsecond line"
    body = _extract_stream_body(pane)
    assert "First assistant line" in body
    assert "second line" in body
