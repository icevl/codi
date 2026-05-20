"""JSONL transcript parser for Codex session files.

Parses Codex session JSONL files and extracts structured messages.
Handles: text, thinking, tool_use, tool_result, local_command, and user messages.
Tool pairing: tool_use blocks in assistant messages are matched with
tool_result blocks in subsequent user messages via tool_use_id.

Shared by both session.py (history) and session_monitor.py (real-time).

Key classes: TranscriptParser (static methods), ParsedEntry, ParsedMessage, PendingToolInfo.
"""

import base64
import difflib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ParsedMessage:
    """Parsed message from a transcript."""

    message_type: str  # "user", "assistant", "tool_use", "tool_result", etc.
    text: str  # Extracted text content
    tool_name: str | None = None  # For tool_use messages


@dataclass
class ParsedEntry:
    """A single parsed message entry ready for display."""

    role: str  # "user" | "assistant"
    text: str  # Already formatted text
    content_type: (
        str  # "text" | "thinking" | "tool_use" | "tool_result" | "local_command"
    )
    tool_use_id: str | None = None
    timestamp: str | None = None  # ISO timestamp from JSONL
    tool_name: str | None = (
        None  # For tool_use entries, the tool name (e.g. "AskUserQuestion")
    )
    tool_input: dict[str, Any] | None = (
        None  # For tool_use entries, the decoded input payload
    )
    image_data: list[tuple[str, bytes]] | None = (
        None  # For tool_result entries with images: (media_type, raw_bytes)
    )
    transcript_offset: int | None = None
    transcript_index: int | None = None


@dataclass
class PendingToolInfo:
    """Information about a pending tool_use waiting for its tool_result."""

    summary: str  # Formatted tool summary (e.g. "**Read**(file.py)")
    tool_name: str  # Tool name (e.g. "Read", "Edit")
    input_data: Any = None  # Tool input parameters (for Edit to generate diff)


class TranscriptParser:
    """Parser for Codex JSONL session files.

    Expected JSONL entry structure:
    - type: "user" | "assistant" | "summary" | "file-history-snapshot" | ...
    - message.content: list of blocks (text, tool_use, tool_result, thinking)
    - sessionId, cwd, timestamp, uuid: metadata fields

    Tool pairing model: tool_use blocks appear in assistant messages,
    matching tool_result blocks appear in the next user message (keyed by tool_use_id).
    """

    # Magic string constants
    _NO_CONTENT_PLACEHOLDER = "(no content)"
    _INTERRUPTED_TEXT = "[Request interrupted by user for tool use]"
    _MAX_SUMMARY_LENGTH = 200
    TRANSCRIPT_OFFSET_KEY = "__transcript_offset"

    @staticmethod
    def parse_line(line: str) -> dict | None:
        """Parse a single JSONL line.

        Args:
            line: A single line from the JSONL file

        Returns:
            Parsed dict or None if line is empty/invalid
        """
        line = line.strip()
        if not line:
            return None

        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _order_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        return None

    @classmethod
    def _get_transcript_offset(cls, data: dict) -> int | None:
        return cls._order_int(data.get(cls.TRANSCRIPT_OFFSET_KEY))

    @staticmethod
    def _stamp_transcript_order(
        result: list[ParsedEntry],
        start_index: int,
        transcript_offset: int | None,
    ) -> None:
        if transcript_offset is None:
            return
        for transcript_index, entry in enumerate(result[start_index:]):
            if entry.transcript_offset is None:
                entry.transcript_offset = transcript_offset
            if entry.transcript_index is None:
                entry.transcript_index = transcript_index

    @staticmethod
    def _extract_response_item_text_blocks(
        payload: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Extract text-like blocks from a modern response_item message payload."""
        content = payload.get("content", [])
        if not isinstance(content, list):
            return []
        blocks: list[dict[str, str]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in ("text", "input_text", "output_text"):
                text = item.get("text", "")
                if isinstance(text, str) and text:
                    blocks.append({"type": "text", "text": text})
        return blocks

    @staticmethod
    def _decode_function_call_arguments(arguments: Any) -> Any:
        """Decode function_call arguments, which can be dict or JSON string."""
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            raw = arguments.strip()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"arguments": arguments}
        return {}

    @classmethod
    def _normalize_entry_for_parsing(cls, data: dict) -> dict | None:
        """Normalize modern transcript records into legacy-like message records.

        Returns a dict with "type" in {"user","assistant"} and a "message.content"
        list, or None when the record should be ignored.
        """
        msg_type = data.get("type")
        if msg_type in ("user", "assistant"):
            # Claude Code marks turn boundaries via `stop_reason: "end_turn"`
            # on the final assistant message. Append a synthetic completion
            # content block so the downstream parser emits a NewMessage
            # with `message_type="completion"` immediately — instead of
            # making us wait for the next user message before the bus
            # learns the turn is done.
            if msg_type == "assistant":
                message = data.get("message")
                if isinstance(message, dict):
                    stop_reason = message.get("stop_reason")
                    if stop_reason in ("end_turn", "stop_sequence"):
                        content = message.get("content")
                        if isinstance(content, list):
                            new_message = {
                                **message,
                                "content": [*content, {"type": "completion"}],
                            }
                            return {**data, "message": new_message}
            return data

        timestamp = data.get("timestamp")

        if msg_type == "response_item":
            payload = data.get("payload")
            if not isinstance(payload, dict):
                return None
            payload_type = payload.get("type")
            if payload_type == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    return None
                return {
                    "type": role,
                    "timestamp": timestamp,
                    "message": {
                        "content": cls._extract_response_item_text_blocks(payload),
                    },
                }
            if payload_type == "function_call":
                call_id = payload.get("call_id") or payload.get("id") or ""
                name = payload.get("name", "unknown")
                input_data = cls._decode_function_call_arguments(
                    payload.get("arguments")
                )
                return {
                    "type": "assistant",
                    "timestamp": timestamp,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": name,
                                "input": input_data,
                            }
                        ]
                    },
                }
            if payload_type == "function_call_output":
                call_id = payload.get("call_id") or ""
                output = payload.get("output", "")
                if isinstance(output, str):
                    output_text = output
                else:
                    output_text = json.dumps(output, ensure_ascii=False)
                return {
                    "type": "user",
                    "timestamp": timestamp,
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": [{"type": "text", "text": output_text}],
                            }
                        ]
                    },
                }
            return None

        if msg_type == "event_msg":
            payload = data.get("payload")
            if not isinstance(payload, dict):
                return None
            payload_type = payload.get("type")
            if payload_type == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict) or item.get("type") != "Plan":
                    return None

                plan_text = item.get("text", "")
                if not isinstance(plan_text, str):
                    plan_text = ""

                return {
                    "type": "assistant",
                    "timestamp": timestamp,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "",
                                "name": "exit_plan_mode",
                                "input": {
                                    "plan": plan_text,
                                    "_source": "item_completed_plan",
                                    "_defer_until_completion": True,
                                    "_skip_fallback_text": True,
                                },
                            }
                        ]
                    },
                }
            if payload_type in ("task_complete", "turn_complete"):
                return {
                    "type": "assistant",
                    "timestamp": timestamp,
                    "message": {"content": [{"type": "completion"}]},
                }
            return None

        return None

    @staticmethod
    def get_message_type(data: dict) -> str | None:
        """Get the message type from parsed data.

        Returns:
            Message type: "user", "assistant", "file-history-snapshot", etc.
        """
        return data.get("type")

    @staticmethod
    def is_user_message(data: dict) -> bool:
        """Check if this is a user message."""
        return data.get("type") == "user"

    @staticmethod
    def extract_text_only(content_list: list[Any]) -> str:
        """Extract only text content from structured content.

        This is used for Telegram notifications where we only want
        the actual text response, not tool calls or thinking.

        Args:
            content_list: List of content blocks

        Returns:
            Combined text content only
        """
        if not isinstance(content_list, list):
            if isinstance(content_list, str):
                return content_list
            return ""

        texts = []
        for item in content_list:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        texts.append(text)

        return "\n".join(texts)

    _RE_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

    _RE_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>")
    _RE_LOCAL_STDOUT = re.compile(
        r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
    )
    _RE_SYSTEM_TAGS = re.compile(
        r"<(bash-input|bash-stdout|bash-stderr|local-command-caveat|system-reminder)"
    )

    @staticmethod
    def _format_edit_diff(old_string: str, new_string: str) -> str:
        """Generate a compact unified diff between old_string and new_string."""
        old_lines = old_string.splitlines(keepends=True)
        new_lines = new_string.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
        # Skip the --- / +++ header lines
        result_lines: list[str] = []
        for line in diff:
            if line.startswith("---") or line.startswith("+++"):
                continue
            # Strip trailing newline for clean display
            result_lines.append(line.rstrip("\n"))
        return "\n".join(result_lines)

    @classmethod
    def format_tool_use_summary(cls, name: str, input_data: dict | Any) -> str:
        """Format a tool_use block into a brief summary line.

        Args:
            name: Tool name (e.g. "Read", "Write", "Bash")
            input_data: The tool input dict

        Returns:
            Formatted string like "**Read**(file.py)"
        """
        if not isinstance(input_data, dict):
            return f"**{name}**"

        # Pick a meaningful short summary based on tool name
        summary = ""
        if name in ("Read", "Glob"):
            summary = input_data.get("file_path") or input_data.get("pattern", "")
        elif name == "Write":
            summary = input_data.get("file_path", "")
        elif name in ("Edit", "NotebookEdit"):
            summary = input_data.get("file_path") or input_data.get("notebook_path", "")
            # Note: Edit/Update diff and stats are generated in tool_result stage,
            # not here. We just show the tool name and file path.
        elif name == "Bash":
            summary = input_data.get("command", "")
        elif name == "Grep":
            summary = input_data.get("pattern", "")
        elif name == "Task":
            summary = input_data.get("description", "")
        elif name == "WebFetch":
            summary = input_data.get("url", "")
        elif name == "WebSearch":
            summary = input_data.get("query", "")
        elif name == "TodoWrite":
            todos = input_data.get("todos", [])
            if isinstance(todos, list):
                summary = f"{len(todos)} item(s)"
        elif name == "TodoRead":
            summary = ""
        elif name in ("AskUserQuestion", "request_user_input"):
            questions = input_data.get("questions", [])
            if isinstance(questions, list) and questions:
                q = questions[0]
                if isinstance(q, dict):
                    summary = q.get("question", "")
        elif name in ("ExitPlanMode", "exit_plan_mode"):
            summary = ""
        elif name == "Skill":
            summary = input_data.get("skill", "")
        else:
            # Generic: show first string value
            for v in input_data.values():
                if isinstance(v, str) and v:
                    summary = v
                    break

        if summary:
            if len(summary) > cls._MAX_SUMMARY_LENGTH:
                summary = summary[: cls._MAX_SUMMARY_LENGTH] + "…"
            return f"**{name}**({summary})"
        return f"**{name}**"

    @staticmethod
    def _format_request_user_prompt(input_data: dict | Any) -> str:
        """Format AskUserQuestion/request_user_input payload for Telegram fallback text."""
        if not isinstance(input_data, dict):
            return ""

        questions = input_data.get("questions", [])
        if not isinstance(questions, list) or not questions:
            return ""

        sections: list[str] = []
        total_questions = len(questions)

        for index, item in enumerate(questions, start=1):
            if not isinstance(item, dict):
                continue

            question_text = item.get("question", "")
            if not isinstance(question_text, str):
                continue
            question_text = question_text.strip()
            if not question_text:
                continue

            lines: list[str] = []
            if total_questions > 1:
                lines.append(f"Question {index}: {question_text}")
            else:
                lines.append(question_text)

            options = item.get("options", [])
            if isinstance(options, list) and options:
                for option_index, option in enumerate(options, start=1):
                    if not isinstance(option, dict):
                        continue
                    label = option.get("label", "")
                    description = option.get("description", "")
                    label = label.strip() if isinstance(label, str) else ""
                    description = (
                        description.strip() if isinstance(description, str) else ""
                    )
                    if label and description:
                        lines.append(f"{option_index}. {label} - {description}")
                    elif label:
                        lines.append(f"{option_index}. {label}")
                    elif description:
                        lines.append(f"{option_index}. {description}")

            if total_questions == 1:
                lines.append("Reply with option number or text.")
            sections.append("\n".join(lines))

        if total_questions > 1 and sections:
            sections.append(
                "Codex is waiting for answers to multiple questions. "
                "Use the prompt controls to move between them."
            )

        return "\n\n".join(sections).strip()

    @staticmethod
    def _format_exit_plan_prompt(input_data: dict | Any) -> str:
        """Format ExitPlanMode/exit_plan_mode payload for Telegram fallback text."""
        if not isinstance(input_data, dict):
            return ""

        plan = input_data.get("plan", "")
        plan_text = plan.strip() if isinstance(plan, str) else ""

        lines: list[str] = []
        if plan_text:
            lines.extend([plan_text, ""])
        lines.extend(
            [
                "Codex is waiting for a plan decision.",
                "1. Yes, implement this plan - Switch to Default and start coding.",
                "2. No, stay in Plan mode - Continue planning with the model.",
            ]
        )
        return "\n".join(lines).strip()

    @staticmethod
    def extract_tool_result_text(content: list | Any) -> str:
        """Extract text from a tool_result content block."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    t = item.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    @staticmethod
    def extract_tool_result_images(
        content: list | Any,
    ) -> list[tuple[str, bytes]] | None:
        """Extract base64-encoded images from a tool_result content block.

        Returns list of (media_type, raw_bytes) tuples, or None if no images found.
        """
        if not isinstance(content, list):
            return None
        images: list[tuple[str, bytes]] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            source = item.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64":
                continue
            media_type = source.get("media_type", "image/png")
            data_str = source.get("data", "")
            if not data_str:
                continue
            try:
                raw_bytes = base64.b64decode(data_str)
                images.append((media_type, raw_bytes))
            except Exception:
                logger.debug("Failed to decode base64 image in tool_result")
        return images if images else None

    @classmethod
    def parse_message(cls, data: dict) -> ParsedMessage | None:
        """Parse a message entry from the JSONL data.

        Args:
            data: Parsed JSON dict from a JSONL line

        Returns:
            ParsedMessage or None if not a parseable message
        """
        msg_type = cls.get_message_type(data)
        if msg_type == "response_item":
            payload = data.get("payload")
            if not isinstance(payload, dict):
                return None
            if payload.get("type") == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    return None
                content_blocks = cls._extract_response_item_text_blocks(payload)
                text = cls.extract_text_only(content_blocks)
                text = cls._RE_ANSI_ESCAPE.sub("", text)
                return ParsedMessage(message_type=role, text=text)
            return None

        if msg_type == "event_msg":
            payload = data.get("payload")
            if not isinstance(payload, dict):
                return None
            payload_type = payload.get("type")
            if payload_type == "agent_message":
                text = payload.get("message", "")
                if isinstance(text, str):
                    return ParsedMessage(message_type="assistant", text=text)
            if payload_type == "user_message":
                text = payload.get("message", "")
                if isinstance(text, str):
                    return ParsedMessage(message_type="user", text=text)
            return None

        if msg_type not in ("user", "assistant"):
            return None

        message = data.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content", "")

        if isinstance(content, list):
            text = cls.extract_text_only(content)
        else:
            text = str(content) if content else ""
        text = cls._RE_ANSI_ESCAPE.sub("", text)

        # Detect local command responses in user messages.
        # These are rendered as bot replies: "❯ /cmd\n  ⎿  output"
        if msg_type == "user" and text:
            stdout_match = cls._RE_LOCAL_STDOUT.search(text)
            if stdout_match:
                stdout = stdout_match.group(1).strip()
                cmd_match = cls._RE_COMMAND_NAME.search(text)
                cmd = cmd_match.group(1) if cmd_match else None
                return ParsedMessage(
                    message_type="local_command",
                    text=stdout,
                    tool_name=cmd,  # reuse field for command name
                )
            # Pure command invocation (no stdout) — carry command name
            cmd_match = cls._RE_COMMAND_NAME.search(text)
            if cmd_match:
                return ParsedMessage(
                    message_type="local_command_invoke",
                    text="",
                    tool_name=cmd_match.group(1),
                )

        return ParsedMessage(
            message_type=msg_type,
            text=text,
        )

    @staticmethod
    def get_timestamp(data: dict) -> str | None:
        """Extract timestamp from message data."""
        return data.get("timestamp")

    EXPANDABLE_QUOTE_START = "\x02EXPQUOTE_START\x02"
    EXPANDABLE_QUOTE_END = "\x02EXPQUOTE_END\x02"

    @classmethod
    def _format_expandable_quote(cls, text: str) -> str:
        """Format text as a Telegram expandable blockquote.

        Wraps text with sentinel markers. The actual MarkdownV2 formatting
        (> prefix, || suffix, escaping) is done in convert_markdown() after
        telegramify processes the surrounding content.
        """
        return f"{cls.EXPANDABLE_QUOTE_START}{text}{cls.EXPANDABLE_QUOTE_END}"

    @classmethod
    def _format_tool_result_text(cls, text: str, tool_name: str | None = None) -> str:
        """Format tool result text with statistics summary.

        Shows relevant statistics for each tool type, with expandable quote for full content.

        No truncation here — per project principles, truncation is handled
        only at the send layer (split_message / _render_expandable_quote).
        """
        if not text:
            return ""

        line_count = text.count("\n") + 1 if text else 0

        # Tool-specific statistics
        if tool_name == "Read":
            # Read: show line count instead of full content
            return f"  ⎿  Read {line_count} lines"

        elif tool_name == "Write":
            # Write: show lines written
            stats = f"  ⎿  Wrote {line_count} lines"
            return stats

        elif tool_name == "Bash":
            # Bash: show output line count
            if line_count > 0:
                stats = f"  ⎿  Output {line_count} lines"
                return stats + "\n" + cls._format_expandable_quote(text)
            return cls._format_expandable_quote(text)

        elif tool_name == "Grep":
            # Grep: show match count (count non-empty lines)
            matches = len([line for line in text.split("\n") if line.strip()])
            stats = f"  ⎿  Found {matches} matches"
            return stats + "\n" + cls._format_expandable_quote(text)

        elif tool_name == "Glob":
            # Glob: show file count
            files = len([line for line in text.split("\n") if line.strip()])
            stats = f"  ⎿  Found {files} files"
            return stats + "\n" + cls._format_expandable_quote(text)

        elif tool_name == "Task":
            # Task: show output length
            if line_count > 0:
                stats = f"  ⎿  Agent output {line_count} lines"
                return stats + "\n" + cls._format_expandable_quote(text)
            return cls._format_expandable_quote(text)

        elif tool_name == "WebFetch":
            # WebFetch: show content length
            char_count = len(text)
            stats = f"  ⎿  Fetched {char_count} characters"
            return stats + "\n" + cls._format_expandable_quote(text)

        elif tool_name == "WebSearch":
            # WebSearch: show results count (estimate by sections)
            results = text.count("\n\n") + 1 if text else 0
            stats = f"  ⎿  {results} search results"
            return stats + "\n" + cls._format_expandable_quote(text)

        # Default: expandable quote without stats
        return cls._format_expandable_quote(text)

    @classmethod
    def parse_entries(
        cls,
        entries: list[dict],
        pending_tools: dict[str, PendingToolInfo] | None = None,
    ) -> tuple[list[ParsedEntry], dict[str, PendingToolInfo]]:
        """Parse a list of JSONL entries into a flat list of display-ready messages.

        This is the shared core logic used by both get_recent_messages (history)
        and check_for_updates (monitor).

        Args:
            entries: List of parsed JSONL dicts (already filtered through parse_line)
            pending_tools: Optional carry-over pending tool_use state from a
                previous call (tool_use_id -> formatted summary). Used by the
                monitor to handle tool_use and tool_result arriving in separate
                poll cycles.

        Returns:
            Tuple of (parsed entries, remaining pending_tools state)
        """
        result: list[ParsedEntry] = []
        last_cmd_name: str | None = None
        # Pending tool_use blocks keyed by id
        _carry_over = pending_tools is not None
        if pending_tools is None:
            pending_tools = {}
        else:
            pending_tools = dict(pending_tools)  # don't mutate caller's dict

        for data in entries:
            entry_offset = cls._get_transcript_offset(data)
            normalized = cls._normalize_entry_for_parsing(data)
            if normalized is None:
                continue
            data = normalized
            msg_type = cls.get_message_type(data)
            if msg_type not in ("user", "assistant"):
                continue

            # Extract timestamp for this entry
            entry_timestamp = cls.get_timestamp(data)

            message = data.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            if not isinstance(content, list):
                content = [{"type": "text", "text": str(content)}] if content else []

            entry_start = len(result)
            parsed = cls.parse_message(data)

            # Handle local command messages first
            if parsed:
                if parsed.message_type == "local_command_invoke":
                    last_cmd_name = parsed.tool_name
                    continue
                if parsed.message_type == "local_command":
                    cmd = parsed.tool_name or last_cmd_name or ""
                    text = parsed.text
                    if cmd:
                        if "\n" in text:
                            formatted = f"❯ `{cmd}`\n```\n{text}\n```"
                        else:
                            formatted = f"❯ `{cmd}`\n`{text}`"
                    else:
                        if "\n" in text:
                            formatted = f"```\n{text}\n```"
                        else:
                            formatted = f"`{text}`"
                    result.append(
                        ParsedEntry(
                            role="assistant",
                            text=formatted,
                            content_type="local_command",
                            timestamp=entry_timestamp,
                        )
                    )
                    last_cmd_name = None
                    cls._stamp_transcript_order(result, entry_start, entry_offset)
                    continue
            last_cmd_name = None

            if msg_type == "assistant":
                # Process content blocks
                has_text = False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")

                    if btype == "text":
                        t = block.get("text", "").strip()
                        if t and t != cls._NO_CONTENT_PLACEHOLDER:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=t,
                                    content_type="text",
                                    timestamp=entry_timestamp,
                                )
                            )
                            has_text = True

                    elif btype == "tool_use":
                        tool_id = block.get("id", "")
                        name = block.get("name", "unknown")
                        inp = block.get("input", {})
                        summary = cls.format_tool_use_summary(name, inp)

                        # AskUserQuestion/request_user_input: emit prompt text so
                        # Telegram always gets the question/options, even when
                        # terminal UI extraction misses this cycle.
                        if name in ("AskUserQuestion", "request_user_input"):
                            prompt_text = cls._format_request_user_prompt(inp)
                            if prompt_text:
                                result.append(
                                    ParsedEntry(
                                        role="assistant",
                                        text=prompt_text,
                                        content_type="text",
                                        timestamp=entry_timestamp,
                                    )
                                )

                        # ExitPlanMode/exit_plan_mode: emit plan content as text
                        # before tool_use entry.
                        if (
                            name in ("ExitPlanMode", "exit_plan_mode")
                            and isinstance(inp, dict)
                            and not inp.get("_skip_fallback_text")
                        ):
                            prompt_text = cls._format_exit_plan_prompt(inp)
                            if prompt_text:
                                result.append(
                                    ParsedEntry(
                                        role="assistant",
                                        text=prompt_text,
                                        content_type="text",
                                        timestamp=entry_timestamp,
                                    )
                                )
                        if tool_id:
                            # Store tool info for later tool_result formatting
                            # Edit tool needs input_data to generate diff in tool_result stage
                            input_data = (
                                inp if name in ("Edit", "NotebookEdit") else None
                            )
                            pending_tools[tool_id] = PendingToolInfo(
                                summary=summary,
                                tool_name=name,
                                input_data=input_data,
                            )
                            # Also emit tool_use entry with tool_name for immediate handling
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=summary,
                                    content_type="tool_use",
                                    tool_use_id=tool_id,
                                    timestamp=entry_timestamp,
                                    tool_name=name,
                                    tool_input=inp if isinstance(inp, dict) else None,
                                )
                            )
                        else:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=summary,
                                    content_type="tool_use",
                                    tool_use_id=tool_id or None,
                                    timestamp=entry_timestamp,
                                    tool_name=name,
                                    tool_input=inp if isinstance(inp, dict) else None,
                                )
                            )

                    elif btype == "thinking":
                        thinking_text = block.get("thinking", "")
                        if thinking_text:
                            quoted = cls._format_expandable_quote(thinking_text)
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=quoted,
                                    content_type="thinking",
                                    timestamp=entry_timestamp,
                                )
                            )
                        elif not has_text:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text="(thinking)",
                                    content_type="thinking",
                                    timestamp=entry_timestamp,
                                )
                            )
                    elif btype == "completion":
                        result.append(
                            ParsedEntry(
                                role="assistant",
                                text="",
                                content_type="completion",
                                timestamp=entry_timestamp,
                            )
                        )

            elif msg_type == "user":
                # Check for tool_result blocks and merge with pending tools
                user_text_parts: list[str] = []

                for block in content:
                    if not isinstance(block, dict):
                        if isinstance(block, str) and block.strip():
                            user_text_parts.append(block.strip())
                        continue
                    btype = block.get("type", "")

                    if btype == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        result_content = block.get("content", "")
                        result_text = cls.extract_tool_result_text(result_content)
                        result_images = cls.extract_tool_result_images(result_content)
                        is_error = block.get("is_error", False)
                        is_interrupted = result_text == cls._INTERRUPTED_TEXT
                        tool_info = pending_tools.pop(tool_use_id, None)
                        _tuid = tool_use_id or None

                        # Extract tool info from PendingToolInfo object
                        if tool_info is None:
                            tool_summary = None
                            tool_name = None
                            tool_input_data = None
                        else:
                            tool_summary = tool_info.summary
                            tool_name = tool_info.tool_name
                            tool_input_data = tool_info.input_data

                        if is_interrupted:
                            # Show interruption inline with tool summary
                            entry_text = tool_summary or ""
                            if entry_text:
                                entry_text += "\n⏹ Interrupted"
                            else:
                                entry_text = "⏹ Interrupted"
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                )
                            )
                        elif is_error:
                            # Show error in stats line
                            if tool_summary:
                                entry_text = tool_summary
                            else:
                                entry_text = "**Error**"
                            # Add error message in stats format
                            if result_text:
                                # Take first line of error as summary
                                error_summary = result_text.split("\n")[0]
                                if len(error_summary) > 100:
                                    error_summary = error_summary[:100] + "…"
                                entry_text += f"\n  ⎿  Error: {error_summary}"
                                # If multi-line error, add expandable quote
                                if "\n" in result_text:
                                    entry_text += "\n" + cls._format_expandable_quote(
                                        result_text
                                    )
                            else:
                                entry_text += "\n  ⎿  Error"
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                )
                            )
                        elif tool_summary:
                            entry_text = tool_summary
                            # For Edit tool, generate diff stats and expandable quote
                            if tool_name == "Edit" and tool_input_data and result_text:
                                old_s = tool_input_data.get("old_string", "")
                                new_s = tool_input_data.get("new_string", "")
                                if old_s and new_s:
                                    diff_text = cls._format_edit_diff(old_s, new_s)
                                    if diff_text:
                                        added = sum(
                                            1
                                            for line in diff_text.split("\n")
                                            if line.startswith("+")
                                            and not line.startswith("+++")
                                        )
                                        removed = sum(
                                            1
                                            for line in diff_text.split("\n")
                                            if line.startswith("-")
                                            and not line.startswith("---")
                                        )
                                        stats = f"  ⎿  Added {added} lines, removed {removed} lines"
                                        entry_text += (
                                            "\n"
                                            + stats
                                            + "\n"
                                            + cls._format_expandable_quote(diff_text)
                                        )
                            # For other tools, append formatted result text
                            elif (
                                result_text
                                and cls.EXPANDABLE_QUOTE_START not in tool_summary
                            ):
                                entry_text += "\n" + cls._format_tool_result_text(
                                    result_text, tool_name
                                )
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=entry_text,
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                )
                            )
                        elif result_text or result_images:
                            result.append(
                                ParsedEntry(
                                    role="assistant",
                                    text=cls._format_tool_result_text(
                                        result_text, tool_name
                                    )
                                    if result_text
                                    else (tool_summary or ""),
                                    content_type="tool_result",
                                    tool_use_id=_tuid,
                                    timestamp=entry_timestamp,
                                    image_data=result_images,
                                )
                            )

                    elif btype == "text":
                        t = block.get("text", "").strip()
                        if t and not cls._RE_SYSTEM_TAGS.search(t):
                            user_text_parts.append(t)

                # Add user text if present (skip if message was only tool_results)
                if user_text_parts:
                    combined = "\n".join(user_text_parts)
                    # Skip if it looks like local command XML
                    if not cls._RE_LOCAL_STDOUT.search(
                        combined
                    ) and not cls._RE_COMMAND_NAME.search(combined):
                        result.append(
                            ParsedEntry(
                                role="user",
                                text=combined,
                                content_type="text",
                                timestamp=entry_timestamp,
                            )
                        )
            cls._stamp_transcript_order(result, entry_start, entry_offset)

        # Flush remaining pending tools at end.
        # In carry-over mode (monitor), keep them pending for the next call
        # without emitting entries. In one-shot mode (history), emit them.
        remaining_pending = dict(pending_tools)
        if not _carry_over:
            for tool_id, tool_info in pending_tools.items():
                result.append(
                    ParsedEntry(
                        role="assistant",
                        text=tool_info.summary,
                        content_type="tool_use",
                        tool_use_id=tool_id,
                    )
                )

        # Strip whitespace
        for entry in result:
            entry.text = entry.text.strip()

        return result, remaining_pending
