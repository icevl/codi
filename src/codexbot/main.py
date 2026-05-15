"""Application entry point for CodexBot.

Configures logging, initializes the tmux session, and starts Telegram polling.
"""

import logging
import sys

from .utils import SingleInstanceLock, codexbot_dir


def main() -> None:
    """Main entry point."""
    lock = SingleInstanceLock(codexbot_dir() / "codexbot.lock")
    if not lock.acquire():
        holder = f" (pid={lock.holder_pid})" if lock.holder_pid else ""
        print(f"Error: another codexbot instance is already running{holder}.")
        sys.exit(1)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    try:
        try:
            from .config import config
        except ValueError as e:
            config_dir = codexbot_dir()
            env_path = config_dir / ".env"
            print(f"Error: {e}\n")
            print(f"Create {env_path} with the following content:\n")
            print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
            print("  ALLOWED_USERS=your_telegram_user_id")
            print()
            print("Get your bot token from @BotFather on Telegram.")
            print("Get your user ID from @userinfobot on Telegram.")
            sys.exit(1)

        root_level = logging._nameToLevel.get(config.log_level, logging.INFO)
        logging.getLogger().setLevel(root_level)
        logging.getLogger("codexbot").setLevel(root_level)
        logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
        logger = logging.getLogger(__name__)

        from .tmux_manager import tmux_manager

        logger.info("Allowed users: %s", config.allowed_users)
        logger.info("Codex sessions path: %s", config.codex_sessions_path)

        session = tmux_manager.get_or_create_session()
        logger.info("Tmux session '%s' ready", session.session_name)

        logger.info("Starting Telegram bot...")
        from .bot import create_bot

        application = create_bot()
        application.run_polling(allowed_updates=["message", "callback_query"])
    finally:
        lock.release()


if __name__ == "__main__":
    main()
