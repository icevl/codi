"""Polls GitHub for new commits on `main` and notifies web clients.

Every `POLL_INTERVAL_SEC` the checker compares `git rev-parse HEAD` of the
local repository against the latest commit SHA on
`https://github.com/icevl/codi/commits/main`. When the SHAs diverge and
the working tree is clean it publishes an `update_available` event on the
shared `EventBus`. The web UI listens for that event, prompts the user,
and on confirmation POSTs to `/api/update/run` which executes
`git pull --ff-only && ./scripts/install_macos_launchd.sh`.

If the working tree is dirty the checker stays silent — running `git pull
--ff-only` over local changes would either fail (conflicts) or move HEAD
past uncommitted work. Either way is worse than just not nudging the
operator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, TypedDict

from .events import EventBus

logger = logging.getLogger(__name__)

REPO = "icevl/codi"
BRANCH = "main"
POLL_INTERVAL_SEC = 600  # 10 minutes — well under the 60/hr unauth GitHub quota.
GITHUB_API = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"


class UpdateStatus(TypedDict):
    current_sha: str | None
    latest_sha: str | None
    has_update: bool
    dirty: bool
    subject: str | None


# Process-global cache: REST status endpoint reads this; the poll loop
# updates it. Plain dict is fine — single asyncio loop, no concurrency.
_state: UpdateStatus = {
    "current_sha": None,
    "latest_sha": None,
    "has_update": False,
    "dirty": False,
    "subject": None,
}


def repo_root() -> Path:
    """Repo root inferred from this file's location."""
    return Path(__file__).resolve().parents[3]


def _git(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_root()),
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None


def get_local_sha() -> str | None:
    return _git(["rev-parse", "HEAD"])


def is_dirty() -> bool:
    out = _git(["status", "--porcelain"])
    return bool(out)


def _fetch_remote_sync() -> tuple[str, str] | None:
    try:
        req = urllib.request.Request(
            GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "codexbot-update-checker",
            },
        )
        # GitHub's public commits API is rate-limited to 60/hr unauthenticated,
        # which is plenty for one poll every 10 min.
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            data: dict[str, Any] = json.load(resp)
        sha = data.get("sha")
        msg = (data.get("commit") or {}).get("message") or ""
        if isinstance(msg, str):
            subject = msg.splitlines()[0] if msg else ""
        else:
            subject = ""
        if isinstance(sha, str):
            return sha, subject
    except Exception as exc:  # noqa: BLE001
        logger.debug("update_checker: fetch failed: %s", exc)
    return None


async def _fetch_remote() -> tuple[str, str] | None:
    return await asyncio.to_thread(_fetch_remote_sync)


async def tick(bus: EventBus) -> None:
    """One poll iteration. Publishes the WS event on rising edge only."""
    current = await asyncio.to_thread(get_local_sha)
    dirty = await asyncio.to_thread(is_dirty)
    remote = await _fetch_remote()
    if remote is None:
        # Network blip — leave previous state alone; the next tick retries.
        return
    latest, subject = remote
    has_update = bool(current and latest != current and not dirty)
    prev = _state["has_update"]
    _state["current_sha"] = current
    _state["latest_sha"] = latest
    _state["subject"] = subject
    _state["dirty"] = dirty
    _state["has_update"] = has_update
    if has_update and not prev:
        await bus.publish(
            {
                "type": "update_available",
                "current_sha": current,
                "latest_sha": latest,
                "subject": subject,
            }
        )


async def poll_loop(bus: EventBus) -> None:
    """Background task: run tick() forever with a 10-minute cadence."""
    # Tick once immediately so the status endpoint has data right after boot.
    while True:
        try:
            await tick(bus)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("update_checker: tick failed")
        await asyncio.sleep(POLL_INTERVAL_SEC)


def get_status() -> UpdateStatus:
    return dict(_state)  # type: ignore[return-value]
