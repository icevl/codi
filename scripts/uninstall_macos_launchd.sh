#!/usr/bin/env bash
set -euo pipefail

LABEL="com.codexbot.bot"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LAUNCH_TARGET="gui/$(id -u)/${LABEL}"

launchctl bootout "${LAUNCH_TARGET}" >/dev/null 2>&1 || true

if [ -f "${PLIST_PATH}" ]; then
    rm -f "${PLIST_PATH}"
    echo "Removed ${PLIST_PATH}"
fi

echo "Stopped and removed ${LABEL}"
