"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

from codexbot.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CODEXBOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_default_codex_command_enables_dangerous_auto_approve(self):
        cfg = Config()
        assert cfg.auto_approve_dangerous is True
        assert (
            cfg.codex_command
            == "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox"
        )

    def test_auto_approve_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("CODEXBOT_AUTO_APPROVE_DANGEROUS", "false")
        cfg = Config()
        assert cfg.auto_approve_dangerous is False
        assert cfg.codex_command == "codex --no-alt-screen"

    def test_no_duplicate_dangerous_flag(self, monkeypatch):
        monkeypatch.setenv(
            "CODEX_COMMAND",
            "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox",
        )
        cfg = Config()
        assert (
            cfg.codex_command
            == "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox"
        )

    def test_deprecated_dangerous_flag_is_normalized(self, monkeypatch):
        monkeypatch.setenv(
            "CODEX_COMMAND", "codex --no-alt-screen --dangerously-skip-permissions"
        )
        monkeypatch.setenv("CODEXBOT_AUTO_APPROVE_DANGEROUS", "false")
        cfg = Config()
        assert (
            cfg.codex_command
            == "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox"
        )

    def test_non_codex_command_not_modified(self, monkeypatch):
        monkeypatch.setenv("CODEX_COMMAND", "my-wrapper codex --no-alt-screen")
        cfg = Config()
        assert cfg.codex_command == "my-wrapper codex --no-alt-screen"

    def test_env_prefixed_codex_command_is_augmented(self, monkeypatch):
        monkeypatch.setenv("CODEX_COMMAND", "IS_SANDBOX=1 codex --no-alt-screen")
        cfg = Config()
        assert (
            cfg.codex_command
            == "IS_SANDBOX=1 codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox"
        )

    def test_custom_tmux_session_name(self, monkeypatch):
        monkeypatch.setenv("TMUX_SESSION_NAME", "mysession")
        cfg = Config()
        assert cfg.tmux_session_name == "mysession"

    def test_custom_monitor_poll_interval(self, monkeypatch):
        monkeypatch.setenv("MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigCodexSessionsPath:
    def test_default_codex_sessions_path(self, monkeypatch):
        monkeypatch.delenv("CODEXBOT_CODEX_SESSIONS_PATH", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        cfg = Config()
        assert cfg.codex_sessions_path == Path.home() / ".codex" / "sessions"

    def test_custom_codex_sessions_path(self, monkeypatch):
        custom_path = "/custom/codex/sessions"
        monkeypatch.setenv("CODEXBOT_CODEX_SESSIONS_PATH", custom_path)
        cfg = Config()
        assert cfg.codex_sessions_path == Path(custom_path)

    def test_codex_home_sessions_path(self, monkeypatch):
        monkeypatch.delenv("CODEXBOT_CODEX_SESSIONS_PATH", raising=False)
        monkeypatch.setenv("CODEX_HOME", "/custom/codex/home")
        cfg = Config()
        assert cfg.codex_sessions_path == Path("/custom/codex/home") / "sessions"

    def test_custom_path_takes_priority_over_codex_home(self, monkeypatch):
        monkeypatch.setenv("CODEXBOT_CODEX_SESSIONS_PATH", "/priority/path")
        monkeypatch.setenv("CODEX_HOME", "/lower/priority")
        cfg = Config()
        assert cfg.codex_sessions_path == Path("/priority/path")


@pytest.mark.usefixtures("_base_env")
class TestConfigOpenAI:
    def test_openai_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = Config()
        assert cfg.openai_api_key == ""
        assert cfg.openai_base_url == "https://api.openai.com/v1"

    def test_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.openai_api_key == "sk-test-123"

    def test_openai_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        cfg = Config()
        assert cfg.openai_base_url == "https://proxy.example.com/v1"

    def test_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("OPENAI_API_KEY") is None

    def test_openai_base_url_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        Config()
        assert os.environ.get("OPENAI_BASE_URL") is None
