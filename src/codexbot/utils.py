"""Shared utility functions used across multiple CodexBot modules.

Provides:
  - codexbot_dir(): resolve config directory from CODEXBOT_DIR env var.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
  - read_cwd_from_jsonl(): extract the cwd field from transcript JSONL.
  - SingleInstanceLock: file lock to prevent multi-instance split-brain.
"""

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

CODEXBOT_DIR_ENV = "CODEXBOT_DIR"


def codexbot_dir() -> Path:
    """Resolve config directory from CODEXBOT_DIR env var or default ~/.codexbot."""
    raw = os.environ.get(CODEXBOT_DIR_ENV, "")
    return Path(raw).expanduser() if raw else Path.home() / ".codexbot"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it
    to the target path. This prevents data corruption if the process
    is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    # Write to temp file in same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_cwd_from_jsonl(file_path: str | Path) -> str:
    """Read the cwd field from the first JSONL entry that has one.

    Shared by session.py and session_monitor.py.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cwd = data.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""


class SingleInstanceLock:
    """Non-blocking file lock used to enforce a single running instance."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh: Any | None = None
        self.holder_pid: int | None = None

    def acquire(self) -> bool:
        """Try to acquire lock. Returns False when another process holds it."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            raw_pid = fh.read().strip()
            try:
                self.holder_pid = int(raw_pid)
            except ValueError:
                self.holder_pid = None
            fh.close()
            return False

        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        self.holder_pid = os.getpid()
        return True

    def release(self) -> None:
        """Release lock if owned by this process."""
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None
