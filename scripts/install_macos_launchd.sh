#!/usr/bin/env bash
set -euo pipefail

LABEL="com.codexbot.bot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
LOG_DIR="${HOME}/.codexbot/logs"

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
TMUX_BIN="${TMUX_BIN:-$(command -v tmux || true)}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
PNPM_BIN="${PNPM_BIN:-$(command -v pnpm || true)}"

if [ -z "${UV_BIN}" ]; then
    echo "uv not found. Install it first: brew install uv"
    exit 1
fi

if [ -z "${TMUX_BIN}" ]; then
    echo "tmux not found. Install it first: brew install tmux"
    exit 1
fi

if [ -z "${CODEX_BIN}" ]; then
    echo "codex not found. Install it first: brew install --cask codex"
    exit 1
fi

mkdir -p "${PLIST_DIR}" "${LOG_DIR}"

# Build the web UI bundle so the SPA is served on the first boot. Skipped
# when pnpm is not installed — the Python backend will fall back to a
# placeholder response until you build manually.
if [ -d "${PROJECT_DIR}/web-ui" ]; then
    if [ -n "${PNPM_BIN}" ]; then
        echo "Building web UI bundle with pnpm…"
        (cd "${PROJECT_DIR}/web-ui" && "${PNPM_BIN}" install --frozen-lockfile && "${PNPM_BIN}" run build)
    else
        echo "pnpm not found — skipping web UI build. Install with: brew install pnpm"
        echo "Then run: cd ${PROJECT_DIR}/web-ui && pnpm install && pnpm build"
    fi
fi

PATH_VALUE="$(dirname "${UV_BIN}"):$(dirname "${TMUX_BIN}"):$(dirname "${CODEX_BIN}"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${UV_BIN}</string>
    <string>run</string>
    <string>codexbot</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${PATH_VALUE}</string>
    <key>CODEXBOT_DIR</key>
    <string>${HOME}/.codexbot</string>
  </dict>

  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
</dict>
</plist>
EOF

plutil -lint "${PLIST_PATH}" >/dev/null

LAUNCH_DOMAIN="gui/$(id -u)"
LAUNCH_TARGET="${LAUNCH_DOMAIN}/${LABEL}"
launchctl bootout "${LAUNCH_TARGET}" >/dev/null 2>&1 || true

# Wait for launchd to fully tear down the previous instance. Bootstrapping
# while the service is still partially registered surfaces as
# "Bootstrap failed: 5: Input/output error".
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! launchctl print "${LAUNCH_TARGET}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.3
done

launchctl enable "${LAUNCH_TARGET}" >/dev/null 2>&1 || true

# Retry bootstrap: macOS occasionally returns I/O error on the first try
# even after teardown completes; usually a second attempt succeeds.
bootstrap_ok=0
for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "${LAUNCH_DOMAIN}" "${PLIST_PATH}" 2>/tmp/codexbot-launchctl.err; then
        bootstrap_ok=1
        break
    fi
    err="$(cat /tmp/codexbot-launchctl.err 2>/dev/null || true)"
    echo "launchctl bootstrap attempt ${attempt} failed: ${err}" >&2
    sleep 1
done
rm -f /tmp/codexbot-launchctl.err
if [ "${bootstrap_ok}" -ne 1 ]; then
    echo "Bootstrap failed after 5 attempts." >&2
    echo "Try: launchctl bootout ${LAUNCH_TARGET}  && sleep 2  && re-run this script" >&2
    exit 1
fi

launchctl enable "${LAUNCH_TARGET}"
launchctl kickstart -k "${LAUNCH_TARGET}"

echo "Installed and started ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Logs:  ${LOG_DIR}/launchd.out.log and ${LOG_DIR}/launchd.err.log"

# Web UI hint
if grep -qE '^[[:space:]]*WEB_UI_PASSWORD=' "${PROJECT_DIR}/.env" 2>/dev/null; then
    WEB_PORT_VALUE="$(grep -E '^[[:space:]]*WEB_UI_PORT=' "${PROJECT_DIR}/.env" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
    : "${WEB_PORT_VALUE:=8787}"
    WEB_HOST_VALUE="$(grep -E '^[[:space:]]*WEB_UI_HOST=' "${PROJECT_DIR}/.env" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
    : "${WEB_HOST_VALUE:=127.0.0.1}"
    echo "Web UI:  http://${WEB_HOST_VALUE}:${WEB_PORT_VALUE} (password from WEB_UI_PASSWORD)"
else
    echo "Web UI:  disabled — set WEB_UI_PASSWORD in ${PROJECT_DIR}/.env to enable, then re-run this script (or 'launchctl kickstart -k ${LAUNCH_TARGET}')."
fi
