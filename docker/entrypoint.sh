#!/usr/bin/env bash
set -euo pipefail

# At least one channel must be configured. Telegram needs both vars,
# web needs WEB_UI_PASSWORD — either is sufficient.
telegram_ok=0
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ALLOWED_USERS:-}" ]; then
    telegram_ok=1
fi
web_ok=0
if [ -n "${WEB_UI_PASSWORD:-}" ]; then
    web_ok=1
fi

if [ "${telegram_ok}" -eq 0 ] && [ "${web_ok}" -eq 0 ]; then
    echo "No channel configured: set TELEGRAM_BOT_TOKEN+ALLOWED_USERS, or WEB_UI_PASSWORD."
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is not available in PATH"
    exit 1
fi

# At least one runtime CLI is required so created sessions can actually start.
if ! command -v codex >/dev/null 2>&1 && ! command -v claude >/dev/null 2>&1; then
    echo "No agent runtime found: install 'codex' or 'claude' in PATH."
    exit 1
fi

mkdir -p "${CODEXBOT_DIR:-/root/.codexbot}"
exec codexbot
