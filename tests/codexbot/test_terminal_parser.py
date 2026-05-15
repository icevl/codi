"""Tests for terminal_parser — regex-based detection of Codex UI elements."""

import pytest

from codexbot.terminal_parser import (
    extract_bash_output,
    extract_interactive_content,
    is_interactive_ui,
    parse_status_line,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_fallback_busy_status_without_chrome(self):
        pane = "output\n• Working (esc to interrupt)\n"
        assert parse_status_line(pane) == "Working (esc to interrupt)"

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Implement this plan?" in result.content
        assert "Continue planning with the model." in result.content

    def test_exit_plan_mode_current_variant(self, sample_pane_exit_plan_current: str):
        result = extract_interactive_content(sample_pane_exit_plan_current)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Implement the plan." in result.content
        assert "Enter to confirm" in result.content

    def test_exit_plan_mode_upstream_snapshot(
        self, sample_pane_exit_plan_upstream: str
    ):
        result = extract_interactive_content(sample_pane_exit_plan_upstream)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Implement this plan?" in result.content
        assert "Press enter to confirm" in result.content

    def test_exit_plan_mode_legacy_variant(self, sample_pane_exit_plan_legacy: str):
        result = extract_interactive_content(sample_pane_exit_plan_legacy)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Codex has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Codex has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_ask_user_current_single(self, sample_pane_ask_user_current_single: str):
        result = extract_interactive_content(sample_pane_ask_user_current_single)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Question 1/1" in result.content
        assert "enter to submit answer" in result.content

    def test_ask_user_current_multi(self, sample_pane_ask_user_current_multi: str):
        result = extract_interactive_content(sample_pane_ask_user_current_multi)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Question 1/2" in result.content
        assert "navigate questions" in result.content

    def test_ask_user_unanswered_confirmation(
        self, sample_pane_ask_user_unanswered_confirmation: str
    ):
        result = extract_interactive_content(
            sample_pane_ask_user_unanswered_confirmation
        )
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Submit with unanswered questions?" in result.content
        assert "Press enter to confirm" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● gpt-5-codex\n"
            "  ○ o4\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ gpt-5-codex\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


class TestClaudeUIPatterns:
    """Claude Code runtime UI detection (Phase 3)."""

    def test_plan_mode_proceed(self):
        pane = (
            "Claude has prepared a plan.\n"
            "\n"
            "Would you like to proceed?\n"
            "\n"
            "❯ 1. Yes, and auto-accept edits\n"
            "  2. Yes, and manually approve edits\n"
            "  3. No, keep planning\n"
        )
        result = extract_interactive_content(pane, runtime="claude")
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "No, keep planning" in result.content

    def test_workspace_trust(self):
        pane = (
            "Do you trust the files in this folder?\n"
            "\n"
            "/Users/mike/foo\n"
            "\n"
            "❯ 1. Yes, proceed\n"
            "  2. No, exit\n"
        )
        result = extract_interactive_content(pane, runtime="claude")
        assert result is not None
        assert result.name == "WorkspaceTrust"

    def test_numbered_menu_fallback(self):
        pane = "Some context line\n\n❯ 1. Option A\n  2. Option B\n  3. Option C\n"
        result = extract_interactive_content(pane, runtime="claude")
        assert result is not None
        # The catch-all NumberedChoice should match when nothing more
        # specific does.
        assert result.name == "NumberedChoice"

    def test_no_ui_returns_none(self):
        assert (
            extract_interactive_content("just plain output\n", runtime="claude") is None
        )

    def test_claude_runtime_does_not_match_codex_wordings(self):
        # Codex-specific wording ("Implement this plan?") should not be
        # mistakenly classified as a Claude ExitPlanMode prompt.
        pane = "Implement this plan?\n\n❯ 1. Yes\n  2. No\n"
        result = extract_interactive_content(pane, runtime="claude")
        # If anything matches at all, it must be the generic catch-all,
        # never the Claude ExitPlanMode pattern.
        if result is not None:
            assert result.name != "ExitPlanMode"


class TestClaudeStartupPrompts:
    def test_bypass_permissions_prompt_matches(self):
        from codexbot.runtimes.claude import _classify_startup_prompt

        pane = (
            "WARNING: Claude Code running in Bypass Permissions mode\n"
            "\n"
            "❯ 1. No, exit\n"
            "  2. Yes, I accept\n"
        )
        assert _classify_startup_prompt(pane) == "bypass_permissions"

    def test_workspace_trust_prompt_matches(self):
        from codexbot.runtimes.claude import _classify_startup_prompt

        pane = (
            "Do you trust the files in this folder?\n"
            "\n"
            "❯ 1. Yes, proceed\n"
            "  2. No, exit\n"
        )
        assert _classify_startup_prompt(pane) == "workspace_trust"

    def test_unrelated_numbered_menu_does_not_match_startup_prompt(self):
        from codexbot.runtimes.claude import _classify_startup_prompt

        pane = "Would you like to proceed?\n\n❯ 1. Yes\n  2. No\n"
        assert _classify_startup_prompt(pane) is None


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")
