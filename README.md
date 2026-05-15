# Codi

[![Repo](https://img.shields.io/badge/github-icevl%2Fcodi-181717?logo=github)](https://github.com/icevl/codi)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> Self-hosted remote control for AI coding agents — Codex and Claude Code running in tmux, driven from a browser or Telegram, in sync.

Codi is a thin control layer over **tmux** that lets you start, monitor, and steer terminal-based AI agents (Codex, Claude Code) from anywhere. Sessions live in tmux on your own machine — Codi just reads their output and sends keystrokes. Two front-ends ship out of the box and stay mirrored:

- a **web UI** (Vite + React) accessible from any browser, and
- a **Telegram bot** with one-topic-per-session forum support.

Everything runs on your hardware. No cloud relay, no agent SDK wrapping, no separate API sessions — the terminal stays the source of truth, and you can `tmux attach` to take over at any time.

---

## Why

AI coding agents work for minutes to hours per turn. When the agent is running and you walk away from the desk, you lose visibility — but the agent doesn't stop. Existing Telegram/web wrappers around the Codex or Claude SDK spin up *separate* API sessions you can't resume locally; existing local-only tools require you to stay at the keyboard.

Codi takes a different cut: the agent runs in a real tmux pane, and Codi is just a multiplexed remote for that pane. Consequences:

- **Switch device mid-conversation.** Start a refactor at the desk in your terminal, monitor it from your phone on the train, take over in the browser at a coffee shop — same session throughout.
- **Run parallel agents.** Each session is a tmux window. Codi orchestrates the list and routes input.
- **No lock-in to the wrapper.** Kill Codi at any time; agents keep running in tmux. Re-attach with `tmux attach -t codi`.
- **Self-hosted by default.** All state lives under `~/.codexbot/` and `~/.codex/` / `~/.claude/`. No third party sees your transcripts.

Codi was developed on itself — iterating from a phone over Telegram while the agent worked at the desk.

---

## Features

### Multi-runtime

- **Codex CLI** and **Claude Code** in the same UI — pick the runtime when creating a session
- Automatic session-ID detection (via `/status` for Codex, transcript probing for Claude)
- Resume previous runs from the project directory's history

### Web UI

- Browser-based control panel at `http://127.0.0.1:8787` (configurable)
- Live transcript stream over WebSocket — assistant text, tool calls, results, thinking blocks
- Live terminal screenshots (ANSI-rendered PNG) for moments you want the raw pane
- Send text, special keys (Esc, arrows, Tab, Ctrl+C…), slash commands (`/clear`, `/compact`, …)
- Drag-and-drop / paste image attachments
- Inline git branch indicator with a one-click branch switcher (`git switch`) for the pane's cwd
- Skill catalog with quick-arm buttons
- Password login + optional TOTP 2FA (QR enrolment on first start)
- Dark theme tuned to GitHub Primer colors

### Telegram bot

- **One topic = one tmux window = one session** in a Forum-enabled chat
- Real-time forwarded messages: assistant replies, thinking, tool use/result, local command output
- Voice messages auto-transcribed via OpenAI Whisper-compatible APIs
- Interactive UI buttons for AskUserQuestion / ExitPlanMode / Permission prompts
- Skill arming via `/skillhelp` and `$skill-name your task` shorthand
- Paginated history browser

### Cross-channel mirroring

Sessions are global to the running Codi instance: a window created in the web UI gets a matching Telegram topic, messages sent in Telegram appear in the web transcript, and either side can drive the same pane. Rename in one channel, see it in the other.

### Operations

- Single-instance lock per `CODEXBOT_DIR` (`codexbot.lock`)
- Persistent state for thread bindings, window display names, read offsets, pinned sessions
- macOS launchd installer and a Docker Compose setup, both for set-and-forget operation

---

## Architecture

```
                +---------------+         +---------------+
                |   Browser     |         |   Telegram    |
                | (Vite + React)|         |    client     |
                +-------+-------+         +-------+-------+
                        |                         |
                        |  HTTPS + WS             |  long-poll
                        |                         |
                +-------v-------------------------v-------+
                |       Codi backend (FastAPI + PTB)      |
                |  - auth (password + TOTP)               |
                |  - session/window registry              |
                |  - tmux I/O bridge                      |
                |  - transcript stream / event bus        |
                +-------+----------------+----------------+
                        |                |
                        v                v
                +---------------+ +---------------+
                |  tmux pane    | |  tmux pane    |  ...one per session
                |  $ codex      | |  $ claude     |
                +---------------+ +---------------+
```

The Python backend uses FastAPI for the web transport and `python-telegram-bot` for Telegram. Both share the same session manager and tmux bridge, which keeps the two channels coherent.

---

## Prerequisites

- **Python** ≥ 3.11 (3.14 tested)
- **tmux** — installed and on `PATH`
- At least one runtime CLI:
  - [**Codex**](https://github.com/openai/codex) — `codex` on `PATH`, signed in (`codex login`)
  - [**Claude Code**](https://github.com/anthropics/claude-code) — `claude` on `PATH`, signed in
- For Telegram: a bot token from [@BotFather](https://t.me/BotFather) with **Threaded Mode** enabled
- For the prebuilt web UI: [pnpm](https://pnpm.io) (only required to build the SPA — the Python backend serves `web-ui/dist/`)

---

## Quick start

### 1. Install

```bash
# Using uv (recommended)
uv tool install git+https://github.com/icevl/codi.git

# Or from source
git clone https://github.com/icevl/codi.git
cd codi
uv sync
```

### 2. Configure

Create `~/.codexbot/.env`:

```ini
# Telegram (optional — set both to enable the bot)
TELEGRAM_BOT_TOKEN=123456:AA...
ALLOWED_USERS=12345678

# Web UI (optional — set a password to enable)
WEB_UI_PASSWORD=change-me
WEB_UI_HOST=127.0.0.1
WEB_UI_PORT=8787
WEB_UI_TOTP_REQUIRED=true

# Voice transcription (optional)
OPENAI_API_KEY=sk-...
```

You need at least one of (a) Telegram credentials, (b) `WEB_UI_PASSWORD` — otherwise Codi has no surface.

### 3. Build the web UI (one-time)

```bash
cd web-ui && pnpm install && pnpm build
```

### 4. Run

```bash
# If installed via uv tool / pipx
codexbot

# If installed from source
uv run codexbot
```

Open the web UI at `http://127.0.0.1:8787` and/or message your Telegram bot.

---

## Configuration

### Required (depending on enabled channel)

| Variable             | Description                                     |
| -------------------- | ----------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (Telegram channel)    |
| `ALLOWED_USERS`      | Comma-separated Telegram user IDs (whitelist)   |
| `WEB_UI_PASSWORD`    | Web UI login password (enables the web channel) |

### Web UI

| Variable                  | Default            | Description                                                              |
| ------------------------- | ------------------ | ------------------------------------------------------------------------ |
| `WEB_UI_HOST`             | `127.0.0.1`        | Listen address. Set to `0.0.0.0` only behind a reverse proxy             |
| `WEB_UI_PORT`             | `8787`             | Listen port                                                              |
| `WEB_UI_SECRET`           | _auto_             | Cookie signing key. Auto-generated and persisted at `~/.codexbot/web_ui_secret` |
| `WEB_UI_TOTP_REQUIRED`    | `true`             | Require Google-Authenticator-style 2FA in addition to the password       |
| `WEB_UI_TOTP_SECRET`      | _auto_             | Pin the TOTP secret (base32). Useful for multi-host deployments          |
| `WEB_UI_TOTP_ISSUER`      | `CodexBot`         | Issuer label shown in the authenticator app                              |
| `WEB_UI_TOTP_ACCOUNT`     | hostname           | Account label shown in the authenticator app                             |
| `WEB_UI_COOKIE_SECURE`    | `auto`             | `auto` (follow scheme), `true`, or `false`                               |
| `WEB_UI_ALLOWED_ORIGINS`  | loopback variants  | Comma-separated WebSocket origin allowlist (full origin with scheme)     |

### Runtimes & sessions

| Variable                            | Default                 | Description                                                       |
| ----------------------------------- | ----------------------- | ----------------------------------------------------------------- |
| `CODEXBOT_DIR`                      | `~/.codexbot`           | Config/state directory; `.env` is loaded from here                |
| `TMUX_SESSION_NAME`                 | `codexbot`              | Tmux session name that hosts all per-agent windows                |
| `CODEX_COMMAND`                     | `codex --no-alt-screen` | Base command for new Codex windows                                |
| `CLAUDE_COMMAND`                    | `claude`                | Base command for new Claude Code windows                          |
| `CODEXBOT_AUTO_APPROVE_DANGEROUS`   | `true`                  | Append `--dangerously-bypass-approvals-and-sandbox` to Codex      |
| `CODEXBOT_LOG_LEVEL`                | `INFO`                  | `DEBUG`, `INFO`, `WARNING`, `ERROR`                               |
| `MONITOR_POLL_INTERVAL`             | `2.0`                   | JSONL polling interval (seconds)                                  |
| `CODEXBOT_STATUS_POLL_INTERVAL`     | `1.0`                   | Status-line polling interval (seconds)                            |
| `CODEXBOT_QUEUE_MAXSIZE`            | `500`                   | Per-topic queue size before backpressure/coalescing               |
| `CODEXBOT_SHOW_HIDDEN_DIRS`         | `false`                 | Show dot-directories in the directory browser                     |
| `OPENAI_API_KEY`                    | _none_                  | API key for voice transcription                                   |
| `OPENAI_BASE_URL`                   | OpenAI default          | Override for compatible providers (Azure, vLLM, etc.)             |

> **Note on auto-approval.** Codex is launched with sandbox + approval bypass by default for unattended operation. Set `CODEXBOT_AUTO_APPROVE_DANGEROUS=false` or override `CODEX_COMMAND` if you prefer to keep prompts.
>
> Codi enforces a single running instance per `CODEXBOT_DIR` using `codexbot.lock` to avoid duplicate event delivery.

---

## Usage

### Web UI

1. Sign in with your password (and TOTP if enabled).
2. Click **+ New session**, pick a directory and a runtime (Codex / Claude), optionally resume a previous run.
3. Type to send to the agent, use the keyboard popover for Esc/arrows/Ctrl+C, slash commands for `/clear` etc.
4. The branch badge under the composer shows the pane's current git branch — click it to switch.

### Telegram (Forum mode)

1. Create a new topic in the Telegram group hosting the bot.
2. Send any message in that topic — a directory browser appears.
3. Pick a directory; if previous sessions exist there, choose to resume or start fresh.
4. From then on, all text and voice in that topic is forwarded to the agent.

Closing the topic kills the tmux window and frees the binding.

**Built-in bot commands:**

| Command       | Description                            |
| ------------- | -------------------------------------- |
| `/start`      | Welcome message                        |
| `/history`    | Paginated message history              |
| `/skillhelp`  | Open the skill button board            |
| `/screenshot` | Capture an ANSI-rendered terminal PNG  |
| `/esc`        | Send Escape to interrupt the agent     |

Anything else (`/clear`, `/compact`, `/cost`, `/review`, …) is forwarded to the agent verbatim.

### Manual tmux

You can also create a window the old-fashioned way; Codi will detect it on the next poll:

```bash
tmux attach -t codexbot
tmux new-window -n myproject -c ~/Code/myproject
codex   # or: claude
```

---

## Deployment

### macOS launchd

```bash
brew install uv tmux pnpm
brew install --cask codex   # or install Claude Code
codex login                 # and/or `claude /login`
./scripts/install_macos_launchd.sh
```

The installer builds the web UI bundle (if `pnpm` is available), writes `~/Library/LaunchAgents/com.codexbot.bot.plist`, and starts the service. Status and logs:

```bash
launchctl print gui/$(id -u)/com.codexbot.bot | head -40
tail -f ~/.codexbot/logs/launchd.err.log
```

Disable: `./scripts/uninstall_macos_launchd.sh`.

### Docker

```bash
brew install --cask docker   # or any Docker host
cp .env.example ~/.codexbot/.env   # then fill it in
docker compose up -d --build
docker compose logs -f codi
```

The web UI is reachable at `http://localhost:8787` (override the host-side port via `WEB_UI_PORT` in `.env`). `~/.codex`, `~/.claude`, and `~/.codexbot` are bind-mounted so state survives container restarts. `~/Projects` is mounted at `/Projects` for the directory browser — point elsewhere with `CODI_PROJECTS_DIR` in `.env`. `restart: unless-stopped` keeps the container alive across reboots.

---

## Data layout

| Path                                | Description                                                                |
| ----------------------------------- | -------------------------------------------------------------------------- |
| `$CODEXBOT_DIR/.env`                | Configuration                                                              |
| `$CODEXBOT_DIR/state.json`          | Thread bindings, window display names, runtime mapping, pinned flags       |
| `$CODEXBOT_DIR/monitor_state.json`  | JSONL byte offsets per session (prevents duplicate notifications)          |
| `$CODEXBOT_DIR/web_ui_secret`       | Auto-generated cookie key (if `WEB_UI_SECRET` is not pinned)               |
| `$CODEXBOT_DIR/web_ui_totp_secret`  | TOTP seed; delete to re-enroll                                             |
| `~/.codex/projects/`                | Codex transcripts (read-only consumer)                                     |
| `~/.claude/projects/`               | Claude Code transcripts (read-only consumer)                               |

---

## Project layout

```
src/codexbot/
├── main.py                 # CLI entry point + bot bootstrap
├── config.py               # Environment-driven config
├── bot.py                  # Telegram bot setup, command handlers, topic routing
├── session.py              # Session manager, state persistence, history
├── session_monitor.py      # JSONL transcript monitoring (polling)
├── monitor_state.py        # Monitor state persistence (byte offsets)
├── tmux_manager.py         # Tmux window lifecycle + key/text sending
├── runtimes/               # Codex and Claude Code adapters
├── handlers/               # Telegram-side handlers (history, queue, directory browser, …)
└── web/                    # FastAPI app + auth + WebSocket event bus
web-ui/                     # Vite + React SPA (TypeScript)
docker/                     # Container entrypoint
scripts/                    # macOS launchd install/uninstall scripts
```

---

## Development

```bash
# Backend
uv sync
uv run codexbot

# Frontend (separate terminal)
cd web-ui && pnpm install && pnpm dev
# Vite serves on :5173 with /api proxied to the Python backend on :8787

# Tests
uv run pytest
```

---

## License

[MIT](LICENSE).
