"""Root conftest — sets env vars BEFORE any codexbot module is imported.

The config.py module-level singleton requires TELEGRAM_BOT_TOKEN and
ALLOWED_USERS at import time, so these must be set before pytest
discovers any test that transitively imports codexbot.
"""

import os
import tempfile

# Force-set (not setdefault) to prevent real env vars from leaking into tests
os.environ["TELEGRAM_BOT_TOKEN"] = "test:0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ALLOWED_USERS"] = "12345"
os.environ["CODEXBOT_DIR"] = tempfile.mkdtemp(prefix="codexbot-test-")
