"""Orchestrate the full Telegram bot setup flow for a mind agent.

The setup flow:
1. Check for stored Telegram user credentials (reused across agents)
2. If none exist, open a browser for the user to log into Telegram
3. Create a new bot via BotFather
4. Inject the bot token into the running agent
5. Persist bot credentials for future reference
"""

import re
import threading
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MindPaths
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import TelegramBotCreationError
from imbue.minds.errors import TelegramCredentialError
from imbue.minds.errors import TelegramCredentialExtractionError
from imbue.minds.telegram.bot_creator import create_telegram_bot
from imbue.minds.telegram.credential_extractor import extract_telegram_credentials_from_browser
from imbue.minds.telegram.credential_store import has_agent_bot_credentials
from imbue.minds.telegram.credential_store import load_agent_bot_credentials
from imbue.minds.telegram.credential_store import load_telegram_user_credentials
from imbue.minds.telegram.credential_store import save_agent_bot_credentials
from imbue.minds.telegram.credential_store import save_telegram_user_credentials
from imbue.minds.telegram.injector import inject_telegram_bot_token
from imbue.mngr.primitives import AgentId

_MAX_BOT_USERNAME_LENGTH: Final[int] = 32

_MIN_BOT_USERNAME_LENGTH: Final[int] = 5


class TelegramSetupStatus(UpperCaseStrEnum):
    """Status of a background Telegram setup operation."""

    CHECKING_CREDENTIALS = auto()
    WAITING_FOR_LOGIN = auto()
    CREATING_BOT = auto()
    INJECTING_CREDENTIALS = auto()
    DONE = auto()
    FAILED = auto()


class TelegramSetupInfo(FrozenModel):
    """Snapshot of a Telegram setup operation's state."""

    agent_id: AgentId = Field(description="ID of the agent being set up")
    status: TelegramSetupStatus = Field(description="Current setup status")
    error: str | None = Field(default=None, description="Error message when status is FAILED")
    bot_username: str | None = Field(default=None, description="Created bot username when status is DONE")


def generate_bot_username(agent_name: str) -> str:
    """Generate a valid Telegram bot username from an agent name.

    Bot usernames must be 5-32 characters, alphanumeric with underscores,
    and end in 'bot'.
    """
    # Sanitize: lowercase, replace non-alphanumeric with underscore
    sanitized = re.sub(r"[^a-z0-9_]", "_", agent_name.lower())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")

    if not sanitized:
        sanitized = "mind"

    username = f"{sanitized}_bot"

    # Truncate if too long (keep room for _bot suffix)
    if len(username) > _MAX_BOT_USERNAME_LENGTH:
        prefix_length = _MAX_BOT_USERNAME_LENGTH - len("_bot")
        username = f"{sanitized[:prefix_length].rstrip('_')}_bot"

    # Pad if too short
    if len(username) < _MIN_BOT_USERNAME_LENGTH:
        username = f"mind_{username}"

    return username


def generate_bot_display_name(agent_name: str) -> str:
    """Generate a human-readable display name for a bot from an agent name."""
    return f"{agent_name} Bot"


class TelegramSetupOrchestrator(MutableModel):
    """Manages background Telegram bot setup for mind agents.

    Thread-safe: all status reads/writes are guarded by an internal lock.
    """

    paths: MindPaths = Field(frozen=True, description="Filesystem paths for minds data")

    _statuses: dict[str, TelegramSetupStatus] = PrivateAttr(default_factory=dict)
    _errors: dict[str, str] = PrivateAttr(default_factory=dict)
    _bot_usernames: dict[str, str] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)

    def start_setup(
        self,
        agent_id: AgentId,
        agent_name: str,
    ) -> None:
        """Start Telegram bot setup for an agent in a background thread.

        If the agent already has bot credentials stored, this is a no-op
        and the status will immediately be DONE.
        """
        aid = str(agent_id)

        # Check if already has credentials
        if has_agent_bot_credentials(self.paths.data_dir, agent_id):
            existing = load_agent_bot_credentials(self.paths.data_dir, agent_id)
            with self._lock:
                self._statuses[aid] = TelegramSetupStatus.DONE
                if existing is not None:
                    self._bot_usernames[aid] = existing.bot_username
            return

        with self._lock:
            self._statuses[aid] = TelegramSetupStatus.CHECKING_CREDENTIALS

        thread = threading.Thread(
            target=self._run_setup_background,
            args=(agent_id, agent_name),
            daemon=True,
            name=f"telegram-setup-{agent_id}",
        )
        thread.start()
        with self._lock:
            self._threads.append(thread)

    def get_setup_info(self, agent_id: AgentId) -> TelegramSetupInfo | None:
        """Get the current setup status for an agent, or None if not tracked."""
        aid = str(agent_id)
        with self._lock:
            status = self._statuses.get(aid)
            if status is None:
                return None
            return TelegramSetupInfo(
                agent_id=agent_id,
                status=status,
                error=self._errors.get(aid),
                bot_username=self._bot_usernames.get(aid),
            )

    def agent_has_telegram(self, agent_id: AgentId) -> bool:
        """Check whether the given agent has Telegram bot credentials."""
        return has_agent_bot_credentials(self.paths.data_dir, agent_id)

    def wait_for_all(self, timeout: float = 10.0) -> None:
        """Wait for all background setup threads to finish."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)

    def _run_setup_background(
        self,
        agent_id: AgentId,
        agent_name: str,
    ) -> None:
        """Background thread that runs the full Telegram setup flow."""
        aid = str(agent_id)
        try:
            with log_span("Setting up Telegram for agent {}", agent_id):
                # Step 1: get or extract user credentials
                user_credentials = load_telegram_user_credentials(self.paths.data_dir)

                if user_credentials is None:
                    with self._lock:
                        self._statuses[aid] = TelegramSetupStatus.WAITING_FOR_LOGIN

                    logger.info("No stored Telegram credentials, opening browser for login...")
                    user_credentials = extract_telegram_credentials_from_browser()
                    save_telegram_user_credentials(self.paths.data_dir, user_credentials)
                else:
                    logger.debug(
                        "Using stored Telegram credentials for {} (DC={})",
                        user_credentials.first_name,
                        user_credentials.dc_id,
                    )

                # Step 2: create a bot via BotFather
                with self._lock:
                    self._statuses[aid] = TelegramSetupStatus.CREATING_BOT

                bot_username = generate_bot_username(agent_name)
                bot_display_name = generate_bot_display_name(agent_name)

                bot_credentials = create_telegram_bot(
                    user_credentials=user_credentials,
                    bot_display_name=bot_display_name,
                    bot_username=bot_username,
                )

                # Step 3: inject into agent
                with self._lock:
                    self._statuses[aid] = TelegramSetupStatus.INJECTING_CREDENTIALS

                inject_telegram_bot_token(
                    agent_id=agent_id,
                    bot_token=bot_credentials.bot_token,
                )

                # Step 4: persist bot credentials
                save_agent_bot_credentials(self.paths.data_dir, agent_id, bot_credentials)

                with self._lock:
                    self._statuses[aid] = TelegramSetupStatus.DONE
                    self._bot_usernames[aid] = bot_credentials.bot_username

                logger.info("Telegram setup complete for agent {}: @{}", agent_id, bot_credentials.bot_username)

        except (
            TelegramCredentialError,
            TelegramCredentialExtractionError,
            TelegramBotCreationError,
            MngrCommandError,
            OSError,
        ) as exc:
            logger.error("Telegram setup failed for agent {}: {}", agent_id, exc)
            with self._lock:
                self._statuses[aid] = TelegramSetupStatus.FAILED
                self._errors[aid] = str(exc)
