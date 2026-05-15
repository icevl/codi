#!/usr/bin/env bash
set -euo pipefail

TMUX_SESSION="codexbot"
TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAX_STOP_WAIT=10   # seconds to wait for process to exit
MAX_START_WAIT=15  # seconds to wait for process to start

# Check if tmux session and window exist
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Error: tmux session '$TMUX_SESSION' does not exist"
    exit 1
fi

if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' 2>/dev/null | grep -qx "$TMUX_WINDOW"; then
    echo "Error: window '$TMUX_WINDOW' not found in session '$TMUX_SESSION'"
    exit 1
fi

get_pane_pid() {
    tmux list-panes -t "$TARGET" -F '#{pane_pid}' | head -n 1
}

get_pane_command() {
    tmux list-panes -t "$TARGET" -F '#{pane_current_command}' | head -n 1
}

is_shell_command() {
    case "${1:-}" in
        bash|zsh|sh|fish|dash) return 0 ;;
        *) return 1 ;;
    esac
}

is_ccbot_running() {
    local pane_cmd
    pane_cmd="$(get_pane_command || true)"
    if [ -z "$pane_cmd" ]; then
        return 1
    fi
    if is_shell_command "$pane_cmd"; then
        return 1
    fi
    case "$pane_cmd" in
        uv|python|python3|codexbot) return 0 ;;
        *) return 0 ;;
    esac
}

wait_until_stopped() {
    local waited=0
    while is_ccbot_running && [ "$waited" -lt "$MAX_STOP_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting for process to exit... (${waited}s/${MAX_STOP_WAIT}s)"
    done
    ! is_ccbot_running
}

wait_until_started() {
    local waited=0
    while ! is_ccbot_running && [ "$waited" -lt "$MAX_START_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting for process to start... (${waited}s/${MAX_START_WAIT}s)"
    done
    is_ccbot_running
}

# Stop existing process if running
if is_ccbot_running; then
    echo "Found running codexbot process, sending Ctrl-C..."
    tmux send-keys -t "$TARGET" C-c

    if ! wait_until_stopped; then
        echo "Process did not exit cleanly, force-resetting pane..."
        tmux respawn-pane -k -t "$TARGET"
        sleep 1
    fi

    echo "Process stopped."
else
    echo "No codexbot process running in $TARGET"
fi

# Brief pause to let the shell settle
sleep 1

# Start codexbot
echo "Starting codexbot in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && uv run codexbot" Enter

# Verify startup and show logs
if wait_until_started; then
    echo "codexbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: codexbot may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
