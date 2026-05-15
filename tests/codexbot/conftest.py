"""Shared fixtures for codexbot unit tests.

Provides factories for building JSONL entries, content blocks,
and sample pane text for terminal parser tests.
"""

import time

import pytest

# ── JSONL entry factories ────────────────────────────────────────────────


@pytest.fixture
def make_jsonl_entry():
    """Factory: build a raw JSONL dict (pre-parse_line)."""

    def _make(
        msg_type: str = "assistant",
        content: list | str = "",
        *,
        timestamp: str | None = None,
        session_id: str = "test-session-id",
        cwd: str = "/tmp/test",
    ) -> dict:
        entry: dict = {
            "type": msg_type,
            "message": {"content": content},
            "sessionId": session_id,
            "cwd": cwd,
        }
        if timestamp:
            entry["timestamp"] = timestamp
        else:
            entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return entry

    return _make


@pytest.fixture
def make_text_block():
    """Factory: build a text content block."""

    def _make(text: str) -> dict:
        return {"type": "text", "text": text}

    return _make


@pytest.fixture
def make_tool_use_block():
    """Factory: build a tool_use content block."""

    def _make(
        tool_id: str = "tool_1",
        name: str = "Read",
        input_data: dict | None = None,
    ) -> dict:
        return {
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": input_data or {},
        }

    return _make


@pytest.fixture
def make_tool_result_block():
    """Factory: build a tool_result content block."""

    def _make(
        tool_use_id: str = "tool_1",
        content: str | list = "result text",
        *,
        is_error: bool = False,
    ) -> dict:
        block: dict = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block

    return _make


@pytest.fixture
def make_thinking_block():
    """Factory: build a thinking content block."""

    def _make(thinking: str = "deep thoughts") -> dict:
        return {"type": "thinking", "thinking": thinking}

    return _make


# ── Sample pane text for terminal parser ─────────────────────────────────


@pytest.fixture
def sample_pane_exit_plan():
    return (
        "  Implement this plan?\n"
        "  Yes, implement this plan\n"
        "  Switch to Default and start coding.\n"
        "  No, stay in Plan mode\n"
        "  Continue planning with the model.\n"
    )


@pytest.fixture
def sample_pane_exit_plan_current():
    return (
        "  Implement the plan.\n"
        "  Yes, implement this plan\n"
        "  Switch to Default and start coding.\n"
        "  No, stay in Plan mode\n"
        "  Enter to confirm · Esc to cancel\n"
    )


@pytest.fixture
def sample_pane_exit_plan_upstream():
    return (
        "  Implement this plan?\n"
        "\n"
        "› 1. Yes, implement this plan  Switch to Default and start coding.\n"
        "  2. No, stay in Plan mode     Continue planning with the model.\n"
        "\n"
        "  Press enter to confirm or esc to go back\n"
    )


@pytest.fixture
def sample_pane_exit_plan_legacy():
    return (
        "  Would you like to proceed?\n"
        "  ─────────────────────────────────\n"
        "  Yes     No\n"
        "  ─────────────────────────────────\n"
        "  ctrl-g to edit in vim\n"
    )


@pytest.fixture
def sample_pane_ask_user_multi_tab():
    return "  ←  ☐ Option A\n     ☐ Option B\n     ☐ Option C\n  Enter to select\n"


@pytest.fixture
def sample_pane_ask_user_single_tab():
    return "  ☐ Option A\n  ☐ Option B\n  Enter to select\n"


@pytest.fixture
def sample_pane_ask_user_current_single():
    return (
        "  Question 1/1 (1 unanswered)\n"
        "  Choose an option.\n"
        "\n"
        "  › 1. Option 1  First choice.\n"
        "    2. Option 2  Second choice.\n"
        "    3. Option 3  Third choice.\n"
        "\n"
        "  tab to add notes | enter to submit answer | esc to interrupt\n"
    )


@pytest.fixture
def sample_pane_ask_user_current_multi():
    return (
        "  Question 1/2 (2 unanswered)\n"
        "  Choose an option.\n"
        "\n"
        "  › 1. Option 1  First choice.\n"
        "    2. Option 2  Second choice.\n"
        "    3. Option 3  Third choice.\n"
        "\n"
        "  tab to add notes | enter to submit answer | ←/→ to navigate questions | esc to interrupt\n"
    )


@pytest.fixture
def sample_pane_ask_user_unanswered_confirmation():
    return (
        "  Submit with unanswered questions?\n"
        "  2 unanswered questions\n"
        "\n"
        "  › 1. Proceed  Submit with 2 unanswered questions.\n"
        "    2. Go back  Return to the first unanswered question.\n"
        "\n"
        "  Press enter to confirm or esc to go back\n"
    )


@pytest.fixture
def sample_pane_permission():
    return "  Do you want to proceed?\n  Some permission details\n  Esc to cancel\n"


_CHROME = (
    "──────────────────────────────────────\n"
    "❯ \n"
    "──────────────────────────────────────\n"
    "  [Opus 4.6] Context: 50%\n"
)


@pytest.fixture
def chrome():
    return _CHROME


@pytest.fixture
def sample_pane_status_line():
    return "Some output text here\nMore output\n✻ Reading file src/main.py\n" + _CHROME


@pytest.fixture
def sample_pane_settings():
    """Realistic Codex /model picker as captured from tmux."""
    return (
        " Select model\n"
        " Switch between Codex models. Applies to this session and future Codex sessions.\n"
        "\n"
        "   1. Default (recommended)  Opus 4.6 · Most capable for complex work\n"
        " ❯ 2. Sonnet                 Sonnet 4.6 · Best for everyday tasks\n"
        "   3. Haiku                  Haiku 4.5 · Fastest for quick answers\n"
        "\n"
        " Use /fast to turn on Fast mode (Opus 4.6 only).\n"
        "\n"
        " Enter to confirm · Esc to exit\n"
    )


@pytest.fixture
def sample_pane_no_ui():
    return "$ echo hello\nhello\n$\n"
