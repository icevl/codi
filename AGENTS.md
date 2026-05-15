# AGENTS.md

Codi is a self-hosted bridge between Codex / Claude Code sessions running in tmux and two front-ends — a browser SPA and a Telegram bot — kept in sync.

## Common Commands

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pyright src/codexbot/
/tmp/codexbot-venv/bin/pytest -q
./scripts/restart.sh
```

## Core Design Constraints

- 1 session = 1 tmux window. Telegram topic and web window both bind to the same tmux window.
- Routing is keyed by tmux window IDs (`@12`), not names.
- Telegram is topic-only; no non-topic fallback logic.
- Multi-runtime: Codex and Claude Code adapters under `src/codexbot/runtimes/`.
- Message truncation happens only at the Telegram send layer.
- Session detection is automatic: `/status` probing + transcript indexing under `~/.codex/sessions` and `~/.claude/projects`.
- Per-user message queue preserves ordering and merges updates safely.
- Web channel uses FastAPI + WebSocket event bus shared with the Telegram path.

## Configuration

- Default state dir: `~/.codexbot/` (override `CODEXBOT_DIR`). Env-var names keep the legacy `CODEXBOT_*` prefix.
- State files: `state.json`, `monitor_state.json`, `web_ui_secret`, `web_ui_totp_secret`.
- Required env vars depend on the enabled channel: Telegram needs `TELEGRAM_BOT_TOKEN` + `ALLOWED_USERS`; web needs `WEB_UI_PASSWORD`.
- Runtime startup overrides: `CODEX_COMMAND`, `CLAUDE_COMMAND`.
