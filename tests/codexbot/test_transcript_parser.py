"""Tests for codexbot.transcript_parser — pure logic, no I/O."""

import pytest

from codexbot.transcript_parser import (
    ParsedMessage,
    TranscriptParser,
)

EXPQUOTE_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXPQUOTE_END = TranscriptParser.EXPANDABLE_QUOTE_END


# ── parse_line ───────────────────────────────────────────────────────────


class TestParseLine:
    @pytest.mark.parametrize(
        "line, expected",
        [
            ('{"type": "user"}', {"type": "user"}),
            ("not-json", None),
            ("", None),
            ("   \t  ", None),
        ],
        ids=["valid_json", "invalid_json", "empty", "whitespace"],
    )
    def test_parse_line(self, line: str, expected: dict | None):
        assert TranscriptParser.parse_line(line) == expected


# ── extract_text_only ────────────────────────────────────────────────────


class TestExtractTextOnly:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("plain string", "plain string"),
            (
                [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
                "hello\nworld",
            ),
            (
                [
                    {"type": "text", "text": "keep"},
                    {"type": "tool_use", "name": "Read"},
                ],
                "keep",
            ),
            ([], ""),
            (42, ""),
        ],
        ids=["string", "text_blocks", "mixed", "empty_list", "non_list_non_string"],
    )
    def test_extract_text_only(self, content: list | str | int, expected: str):
        assert TranscriptParser.extract_text_only(content) == expected


# ── format_tool_use_summary ──────────────────────────────────────────────


class TestFormatToolUseSummary:
    @pytest.mark.parametrize(
        "name, input_data, expected",
        [
            ("Read", {"file_path": "src/main.py"}, "**Read**(src/main.py)"),
            ("Write", {"file_path": "out.txt"}, "**Write**(out.txt)"),
            ("Bash", {"command": "ls -la"}, "**Bash**(ls -la)"),
            ("Grep", {"pattern": "TODO"}, "**Grep**(TODO)"),
            ("Glob", {"pattern": "*.py"}, "**Glob**(*.py)"),
            ("Task", {"description": "analyze code"}, "**Task**(analyze code)"),
            (
                "WebFetch",
                {"url": "https://example.com"},
                "**WebFetch**(https://example.com)",
            ),
            ("WebSearch", {"query": "python async"}, "**WebSearch**(python async)"),
            ("TodoWrite", {"todos": [1, 2, 3]}, "**TodoWrite**(3 item(s))"),
            ("TodoRead", {}, "**TodoRead**"),
            (
                "AskUserQuestion",
                {"questions": [{"question": "Continue?"}]},
                "**AskUserQuestion**(Continue?)",
            ),
            (
                "request_user_input",
                {"questions": [{"question": "Continue?"}]},
                "**request_user_input**(Continue?)",
            ),
            ("ExitPlanMode", {}, "**ExitPlanMode**"),
            ("exit_plan_mode", {}, "**exit_plan_mode**"),
            ("Skill", {"skill": "code-review"}, "**Skill**(code-review)"),
            (
                "CustomTool",
                {"first_key": "value1"},
                "**CustomTool**(value1)",
            ),
        ],
        ids=[
            "Read",
            "Write",
            "Bash",
            "Grep",
            "Glob",
            "Task",
            "WebFetch",
            "WebSearch",
            "TodoWrite",
            "TodoRead",
            "AskUserQuestion",
            "request_user_input",
            "ExitPlanMode",
            "exit_plan_mode",
            "Skill",
            "unknown_tool",
        ],
    )
    def test_tool_summary(self, name: str, input_data: dict, expected: str):
        assert TranscriptParser.format_tool_use_summary(name, input_data) == expected

    def test_non_dict_input(self):
        assert (
            TranscriptParser.format_tool_use_summary("Read", "not a dict") == "**Read**"
        )

    def test_truncation_at_200_chars(self):
        long_value = "x" * 250
        result = TranscriptParser.format_tool_use_summary(
            "Bash", {"command": long_value}
        )
        assert len(long_value) > 200
        assert result == f"**Bash**({'x' * 200}…)"


# ── extract_tool_result_text ─────────────────────────────────────────────


class TestExtractToolResultText:
    @pytest.mark.parametrize(
        "content, expected",
        [
            ("raw string", "raw string"),
            (
                [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}],
                "line1\nline2",
            ),
            (
                [{"type": "text", "text": "keep"}, {"type": "image", "data": "..."}],
                "keep",
            ),
            (None, ""),
        ],
        ids=["string", "text_blocks", "mixed", "none"],
    )
    def test_extract_tool_result_text(self, content: str | list | None, expected: str):
        assert TranscriptParser.extract_tool_result_text(content) == expected


# ── parse_message ────────────────────────────────────────────────────────


class TestParseMessage:
    def test_user_text(self):
        data = {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="user", text="hello")

    def test_assistant_text(self):
        data = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi there"}]},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="assistant", text="hi there")

    def test_local_command_with_stdout(self):
        data = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<command-name>/help</command-name>"
                            "<local-command-stdout>Available commands</local-command-stdout>"
                        ),
                    }
                ]
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result is not None
        assert result.message_type == "local_command"
        assert result.text == "Available commands"
        assert result.tool_name == "/help"

    def test_local_command_invoke(self):
        data = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "<command-name>/clear</command-name>"}
                ]
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result is not None
        assert result.message_type == "local_command_invoke"
        assert result.text == ""
        assert result.tool_name == "/clear"

    def test_non_user_assistant_returns_none(self):
        data = {
            "type": "summary",
            "message": {"content": "summary text"},
        }
        assert TranscriptParser.parse_message(data) is None

    def test_string_content(self):
        data = {
            "type": "assistant",
            "message": {"content": "plain response"},
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="assistant", text="plain response")

    def test_response_item_assistant_message(self):
        data = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "modern assistant"}],
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(
            message_type="assistant", text="modern assistant"
        )

    def test_response_item_user_message(self):
        data = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "modern user"}],
            },
        }
        result = TranscriptParser.parse_message(data)
        assert result == ParsedMessage(message_type="user", text="modern user")


# ── _format_edit_diff ────────────────────────────────────────────────────


class TestFormatEditDiff:
    @pytest.mark.parametrize(
        "old, new, check",
        [
            (
                "hello",
                "world",
                lambda r: "-hello" in r and "+world" in r,
            ),
            (
                "line1\nline2\nline3",
                "line1\nchanged\nline3",
                lambda r: "-line2" in r and "+changed" in r,
            ),
            (
                "same",
                "same",
                lambda r: r == "",
            ),
        ],
        ids=["single_line", "multi_line", "identical"],
    )
    def test_format_edit_diff(self, old: str, new: str, check):
        result = TranscriptParser._format_edit_diff(old, new)
        assert check(result), f"Check failed for ({old!r}, {new!r}): {result!r}"


# ── _format_tool_result_text ─────────────────────────────────────────────


class TestFormatToolResultText:
    @pytest.mark.parametrize(
        "text, tool_name, check",
        [
            (
                "line1\nline2\nline3",
                "Read",
                lambda r: r == "  ⎿  Read 3 lines",
            ),
            (
                "line1\nline2",
                "Write",
                lambda r: r == "  ⎿  Wrote 2 lines",
            ),
            (
                "output line",
                "Bash",
                lambda r: (
                    r.startswith("  ⎿  Output 1 lines")
                    and EXPQUOTE_START in r
                    and EXPQUOTE_END in r
                ),
            ),
            (
                "file1.py\nfile2.py\n",
                "Grep",
                lambda r: "Found 2 matches" in r and EXPQUOTE_START in r,
            ),
            (
                "a.py\nb.py\nc.py",
                "Glob",
                lambda r: "Found 3 files" in r and EXPQUOTE_START in r,
            ),
            (
                "agent says hello",
                "Task",
                lambda r: "Agent output 1 lines" in r and EXPQUOTE_START in r,
            ),
            (
                "page content here",
                "WebFetch",
                lambda r: (
                    f"Fetched {len('page content here')} characters" in r
                    and EXPQUOTE_START in r
                ),
            ),
            (
                "",
                "Read",
                lambda r: r == "",
            ),
        ],
        ids=["Read", "Write", "Bash", "Grep", "Glob", "Task", "WebFetch", "empty"],
    )
    def test_format_tool_result_text(self, text: str, tool_name: str, check):
        result = TranscriptParser._format_tool_result_text(text, tool_name)
        assert check(result), f"Failed check for {tool_name!r}: {result!r}"


# ── parse_entries ────────────────────────────────────────────────────────


class TestParseEntries:
    def test_assistant_text(self, make_jsonl_entry, make_text_block):
        entries = [make_jsonl_entry("assistant", [make_text_block("Hello!")])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "assistant"
        assert result[0].text == "Hello!"
        assert result[0].content_type == "text"

    def test_user_text(self, make_jsonl_entry, make_text_block):
        entries = [make_jsonl_entry("user", [make_text_block("Hi bot")])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "user"
        assert result[0].text == "Hi bot"

    def test_transcript_order_metadata(self, make_jsonl_entry, make_text_block):
        first = make_jsonl_entry(
            "assistant", [make_text_block("one"), make_text_block("two")]
        )
        first[TranscriptParser.TRANSCRIPT_OFFSET_KEY] = 42
        second = make_jsonl_entry("user", [make_text_block("three")])
        second[TranscriptParser.TRANSCRIPT_OFFSET_KEY] = 99

        result, pending = TranscriptParser.parse_entries([first, second])

        assert pending == {}
        assert [(e.text, e.transcript_offset, e.transcript_index) for e in result] == [
            ("one", 42, 0),
            ("two", 42, 1),
            ("three", 99, 0),
        ]

    def test_tool_use_and_result_pairing(
        self,
        make_jsonl_entry,
        make_text_block,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "app.py"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "file contents line1\nline2\nline3")],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_use_entries = [e for e in result if e.content_type == "tool_use"]
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_use_entries) == 1
        assert tool_use_entries[0].tool_use_id == "t1"
        assert "**Read**" in tool_use_entries[0].text
        assert len(tool_result_entries) == 1
        assert tool_result_entries[0].tool_use_id == "t1"
        assert not pending

    def test_thinking_block(self, make_jsonl_entry, make_thinking_block):
        entries = [
            make_jsonl_entry("assistant", [make_thinking_block("reasoning here")])
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].content_type == "thinking"
        assert EXPQUOTE_START in result[0].text
        assert EXPQUOTE_END in result[0].text
        assert "reasoning here" in result[0].text

    def test_local_command_with_stdout(self, make_jsonl_entry, make_text_block):
        xml = (
            "<command-name>/status</command-name>"
            "<local-command-stdout>all good</local-command-stdout>"
        )
        entries = [make_jsonl_entry("user", [make_text_block(xml)])]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].content_type == "local_command"
        assert "/status" in result[0].text
        assert "all good" in result[0].text

    def test_exit_plan_mode_emits_plan(self, make_jsonl_entry, make_tool_use_block):
        block = make_tool_use_block(
            "t1", "ExitPlanMode", {"plan": "Step 1: do X\nStep 2: do Y"}
        )
        entries = [make_jsonl_entry("assistant", [block])]
        result, pending = TranscriptParser.parse_entries(entries)
        texts = [e for e in result if e.content_type == "text"]
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert len(texts) == 1
        assert "Step 1: do X" in texts[0].text
        assert "Codex is waiting for a plan decision." in texts[0].text
        assert "1. Yes, implement this plan" in texts[0].text
        assert len(tool_uses) >= 1

    def test_exit_plan_mode_snake_case_emits_plan(
        self, make_jsonl_entry, make_tool_use_block
    ):
        block = make_tool_use_block(
            "t1", "exit_plan_mode", {"plan": "Step 1: do X\nStep 2: do Y"}
        )
        entries = [make_jsonl_entry("assistant", [block])]
        result, pending = TranscriptParser.parse_entries(entries)
        texts = [e for e in result if e.content_type == "text"]
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert len(texts) == 1
        assert "Step 1: do X" in texts[0].text
        assert "2. No, stay in Plan mode" in texts[0].text
        assert len(tool_uses) >= 1

    def test_request_user_input_emits_question_text(
        self, make_jsonl_entry, make_tool_use_block
    ):
        block = make_tool_use_block(
            "t1",
            "request_user_input",
            {
                "questions": [
                    {
                        "question": "Choose rollout mode",
                        "options": [
                            {"label": "Canary", "description": "Slow rollout"},
                            {"label": "All users", "description": "Immediate rollout"},
                        ],
                    }
                ]
            },
        )
        entries = [make_jsonl_entry("assistant", [block])]
        result, pending = TranscriptParser.parse_entries(entries)
        texts = [e for e in result if e.content_type == "text"]
        tool_uses = [e for e in result if e.content_type == "tool_use"]
        assert len(texts) == 1
        assert "Choose rollout mode" in texts[0].text
        assert "1. Canary - Slow rollout" in texts[0].text
        assert "2. All users - Immediate rollout" in texts[0].text
        assert "Reply with option number or text." in texts[0].text
        assert len(tool_uses) >= 1
        assert tool_uses[0].tool_name == "request_user_input"

    def test_request_user_input_multi_question_emits_navigation_hint(
        self, make_jsonl_entry, make_tool_use_block
    ):
        block = make_tool_use_block(
            "t1",
            "request_user_input",
            {
                "questions": [
                    {
                        "question": "Choose rollout mode",
                        "options": [
                            {"label": "Canary", "description": "Slow rollout"},
                            {"label": "All users", "description": "Immediate rollout"},
                        ],
                    },
                    {
                        "question": "Choose approval policy",
                        "options": [
                            {"label": "Ask", "description": "Prompt for approval"},
                            {"label": "Auto", "description": "Proceed automatically"},
                        ],
                    },
                ]
            },
        )
        entries = [make_jsonl_entry("assistant", [block])]
        result, pending = TranscriptParser.parse_entries(entries)
        texts = [e for e in result if e.content_type == "text"]
        assert len(texts) == 1
        assert "Question 1: Choose rollout mode" in texts[0].text
        assert "Question 2: Choose approval policy" in texts[0].text
        assert "Use the prompt controls to move between them." in texts[0].text
        assert "Reply with option number or text." not in texts[0].text

    def test_edit_tool_diff_stats(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        edit_input = {
            "file_path": "main.py",
            "old_string": "old line",
            "new_string": "new line",
        }
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Edit", edit_input)],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "OK")],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        tr = tool_result_entries[0]
        assert "Added" in tr.text
        assert "removed" in tr.text
        assert EXPQUOTE_START in tr.text

    def test_error_tool_result(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "rm -rf /"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "Permission denied", is_error=True)],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        assert "Error: Permission denied" in tool_result_entries[0].text

    def test_interrupted_tool_result(
        self,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "x.py"})],
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", TranscriptParser._INTERRUPTED_TEXT)],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_result_entries) == 1
        assert "Interrupted" in tool_result_entries[0].text

    def test_pending_tools_carry_over(self, make_jsonl_entry, make_tool_use_block):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries, pending_tools={})
        assert "t1" in pending
        flushed = [
            e for e in result if e.content_type == "tool_use" and e.tool_use_id == "t1"
        ]
        assert len(flushed) == 1

    def test_pending_tools_flushed_without_carry_over(
        self, make_jsonl_entry, make_tool_use_block
    ):
        entries = [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries, pending_tools=None)
        tool_entries = [e for e in result if e.tool_use_id == "t1"]
        assert len(tool_entries) == 2
        assert tool_entries[0].content_type == "tool_use"
        assert tool_entries[1].content_type == "tool_use"

    def test_system_tag_filtered(self, make_jsonl_entry, make_text_block):
        entries = [
            make_jsonl_entry(
                "user",
                [
                    make_text_block(
                        "<system-reminder>secret instructions</system-reminder>"
                    )
                ],
            ),
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        user_entries = [e for e in result if e.role == "user"]
        assert len(user_entries) == 0

    def test_parse_response_item_message_modern_format(self):
        entries = [
            {
                "type": "response_item",
                "timestamp": "2026-03-05T07:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Hello from modern format"}
                    ],
                },
            }
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        assert len(result) == 1
        assert result[0].role == "assistant"
        assert result[0].content_type == "text"
        assert result[0].text == "Hello from modern format"
        assert pending == {}

    def test_parse_function_call_and_output_modern_format(self):
        entries = [
            {
                "type": "response_item",
                "timestamp": "2026-03-05T07:00:01Z",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_123",
                    "arguments": '{"cmd":"pwd"}',
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-03-05T07:00:02Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "/Users/vlupanov/Documents/codexbot\n",
                },
            },
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        tool_use_entries = [e for e in result if e.content_type == "tool_use"]
        tool_result_entries = [e for e in result if e.content_type == "tool_result"]
        assert len(tool_use_entries) == 1
        assert tool_use_entries[0].tool_use_id == "call_123"
        assert "exec_command" in tool_use_entries[0].text
        assert len(tool_result_entries) == 1
        assert tool_result_entries[0].tool_use_id == "call_123"
        assert pending == {}

    def test_parse_task_complete_as_completion_entry(self):
        entries = [
            {
                "type": "event_msg",
                "timestamp": "2026-03-05T07:00:03Z",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "019c-abc",
                    "last_agent_message": "Done",
                },
            }
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        completion_entries = [e for e in result if e.content_type == "completion"]
        assert len(completion_entries) == 1
        assert completion_entries[0].role == "assistant"
        assert completion_entries[0].text == ""
        assert pending == {}

    def test_parse_turn_complete_alias_as_completion_entry(self):
        entries = [
            {
                "type": "event_msg",
                "timestamp": "2026-03-05T07:00:03Z",
                "payload": {
                    "type": "turn_complete",
                    "turn_id": "019c-abc",
                    "last_agent_message": "Done",
                },
            }
        ]
        result, pending = TranscriptParser.parse_entries(entries)
        completion_entries = [e for e in result if e.content_type == "completion"]
        assert len(completion_entries) == 1
        assert completion_entries[0].role == "assistant"
        assert completion_entries[0].text == ""
        assert pending == {}

    def test_parse_plan_item_completed_as_exit_plan_prompt(self):
        entries = [
            {
                "type": "event_msg",
                "timestamp": "2026-03-06T10:00:00Z",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-1",
                    "item": {
                        "type": "Plan",
                        "id": "turn-1-plan",
                        "text": "# Plan\n\nDo the work.",
                    },
                },
            }
        ]

        result, pending = TranscriptParser.parse_entries(entries)

        assert [entry.content_type for entry in result] == ["tool_use"]
        tool_entry = result[0]
        assert tool_entry.role == "assistant"
        assert tool_entry.tool_name == "exit_plan_mode"
        assert tool_entry.tool_input is not None
        assert tool_entry.tool_input["plan"] == "# Plan\n\nDo the work."
        assert tool_entry.tool_input["_source"] == "item_completed_plan"
        assert tool_entry.tool_input["_defer_until_completion"] is True
        assert pending == {}
