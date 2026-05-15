"""Terminal output parser — detects Codex UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Codex text patterns live here. To support a new UI type or
a changed Codex version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output().
"""

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Codex
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Implement this plan\?"),
            re.compile(r"^\s*Implement the plan\."),
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Codex has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*Continue planning with the model\.?"),
            re.compile(r"^\s*(?:Press )?enter to confirm\b", re.IGNORECASE),
            re.compile(r"^\s*Enter to confirm\b"),
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*to edit in external editor"),
            re.compile(r"^\s*Esc to (cancel|exit|go back)", re.IGNORECASE),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*Question \d+/\d+\b"),),
        bottom=(
            re.compile(r"\benter to submit answer\b", re.IGNORECASE),
            re.compile(r"\benter to submit all\b", re.IGNORECASE),
            re.compile(r"\besc to interrupt\b", re.IGNORECASE),
        ),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*Submit with unanswered questions\?"),),
        bottom=(
            re.compile(
                r"^\s*Press enter to confirm or esc to go back\b", re.IGNORECASE
            ),
        ),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Claude Code UI patterns ──────────────────────────────────────────────
#
# Claude Code's interactive prompts are mostly numbered "❯ 1. ..." menus
# with a question line above. Coverage here focuses on the wordings that
# differ from Codex (plan mode confirm, workspace trust, compaction) plus
# the generic numbered-menu fallback. Many Codex patterns work for Claude
# too (the "❯ 1. Yes" matcher, "Do you want to make this edit", etc.) so
# we reuse them where applicable.

CLAUDE_UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            re.compile(r"^\s*Ready to code\?"),
        ),
        bottom=(
            re.compile(r"^\s*❯?\s*\d+\.\s+No,\s+keep\s+planning", re.IGNORECASE),
            re.compile(r"^\s*esc to (cancel|exit|go back)", re.IGNORECASE),
            re.compile(r"^\s*Enter to confirm", re.IGNORECASE),
        ),
        min_gap=2,
    ),
    UIPattern(
        name="WorkspaceTrust",
        top=(re.compile(r"^\s*Do you trust the files in this folder\?"),),
        bottom=(
            re.compile(r"^\s*❯?\s*\d+\.\s+(No,\s+exit|Yes,\s+proceed)", re.IGNORECASE),
        ),
        min_gap=2,
    ),
    UIPattern(
        name="CompactConfirm",
        top=(
            re.compile(r"^\s*Are you sure you want to compact"),
            re.compile(r"^\s*Compact this conversation\?"),
        ),
        bottom=(re.compile(r"^\s*❯?\s*\d+\.\s+(Yes|No)\b", re.IGNORECASE),),
        min_gap=1,
    ),
    UIPattern(
        # Generic permission prompt — mirrors the Codex wordings; Claude uses
        # similar phrasing for Edit/Write/Bash approvals.
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
            re.compile(r"^\s*Do you want to run "),
        ),
        bottom=(
            re.compile(r"^\s*\d+\.\s+No,\s+and\s+tell\s+Claude", re.IGNORECASE),
            re.compile(r"^\s*esc to (cancel|exit)", re.IGNORECASE),
        ),
        min_gap=1,
    ),
    UIPattern(
        # Catch-all for numbered-choice menus with no question line we
        # matched above. The ❯ marker on "1." is the strongest signal that
        # the TUI is currently waiting for a numeric selection.
        name="NumberedChoice",
        top=(re.compile(r"^\s*❯\s*1\.\s+\S"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Radio-button picker used by AskUserQuestion in Claude.
        name="AskUserQuestion",
        top=(re.compile(r"^\s*❯\s*[◯◉○●]\s+"),),
        bottom=(
            re.compile(r"^\s*Enter to (confirm|submit)", re.IGNORECASE),
            re.compile(r"^\s*esc to (cancel|exit)", re.IGNORECASE),
        ),
        min_gap=1,
    ),
]


def _patterns_for_runtime(runtime: str | None) -> list[UIPattern]:
    if runtime == "claude":
        return CLAUDE_UI_PATTERNS
    return UI_PATTERNS


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(
    pane_text: str, runtime: str | None = None
) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern for the given runtime (default: Codex patterns)
    in declaration order; first match wins. Returns None if no recognizable
    interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in _patterns_for_runtime(runtime):
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str, runtime: str | None = None) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text, runtime=runtime) is not None


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Codex uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])
STATUS_PREFIX_CHARS = STATUS_SPINNERS.union({"•", "●", "○"})
_RE_ESC_TO_INTERRUPT = re.compile(r"\besc to interrupt\b", re.IGNORECASE)


def _parse_status_line_without_chrome(lines: list[str]) -> str | None:
    """Fallback status parser for newer Codex UIs without separator chrome."""
    search_start = max(0, len(lines) - 8)
    for i in range(len(lines) - 1, search_start - 1, -1):
        line = lines[i].strip()
        if not line:
            continue
        if not _RE_ESC_TO_INTERRUPT.search(line):
            continue
        if line[0] in STATUS_PREFIX_CHARS:
            return line[1:].strip()
        return line
    return None


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Codex status line from terminal output.

    The status line (spinner + working text) appears immediately above
    the chrome separator (a full line of ``─`` characters).  We locate
    the separator first, then check the line just above it — this avoids
    false positives from ``·`` bullets in regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Find the chrome separator: topmost ──── line in the last 10 lines
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break

    if chrome_idx is None:
        return _parse_status_line_without_chrome(lines)

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Codex's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Codex's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Codex's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
