"""Persistent storage for Telegram credentials.

Stores user credentials (for authentication with Telegram) and per-agent
bot credentials in the minds data directory. User credentials are shared
across all agents; bot credentials are stored per agent.
"""

import json
from pathlib import Path

from loguru import logger
from pydantic import SecretStr
from pydantic import ValidationError

from imbue.imbue_common.logging import log_span
from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.data_types import TelegramUserCredentials
from imbue.mngr.primitives import AgentId


def _telegram_dir(data_dir: Path) -> Path:
    return data_dir / "telegram"


def _user_credentials_path(data_dir: Path) -> Path:
    return _telegram_dir(data_dir) / "user_credentials.json"


def _bot_credentials_path(data_dir: Path, agent_id: AgentId) -> Path:
    return _telegram_dir(data_dir) / "bots" / f"{agent_id}.json"


def load_telegram_user_credentials(data_dir: Path) -> TelegramUserCredentials | None:
    """Load stored Telegram user credentials, or None if not yet saved."""
    creds_path = _user_credentials_path(data_dir)
    if not creds_path.exists():
        return None

    try:
        raw = json.loads(creds_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load Telegram user credentials from {}: {}", creds_path, exc)
        return None

    try:
        return TelegramUserCredentials.model_validate(raw)
    except ValidationError as exc:
        logger.warning("Telegram user credentials file has invalid schema ({}): {}", creds_path, exc)
        return None


def save_telegram_user_credentials(
    data_dir: Path,
    credentials: TelegramUserCredentials,
) -> None:
    """Save Telegram user credentials to disk for future reuse."""
    creds_path = _user_credentials_path(data_dir)
    with log_span("Saving Telegram user credentials to {}", creds_path):
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        creds_path.write_text(credentials.model_dump_json(indent=2))


def load_agent_bot_credentials(
    data_dir: Path,
    agent_id: AgentId,
) -> TelegramBotCredentials | None:
    """Load stored bot credentials for a specific agent, or None if not set up."""
    creds_path = _bot_credentials_path(data_dir, agent_id)
    if not creds_path.exists():
        return None

    try:
        raw = json.loads(creds_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load bot credentials for agent {}: {}", agent_id, exc)
        return None

    # SecretStr needs special handling when loading from JSON
    if "bot_token" in raw and isinstance(raw["bot_token"], str):
        raw["bot_token"] = SecretStr(raw["bot_token"])

    try:
        return TelegramBotCredentials.model_validate(raw)
    except ValidationError as exc:
        logger.warning("Bot credentials file has invalid schema for agent {} ({}): {}", agent_id, creds_path, exc)
        return None


def save_agent_bot_credentials(
    data_dir: Path,
    agent_id: AgentId,
    credentials: TelegramBotCredentials,
) -> None:
    """Save bot credentials for a specific agent."""
    creds_path = _bot_credentials_path(data_dir, agent_id)
    with log_span("Saving bot credentials for agent {} to {}", agent_id, creds_path):
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        # Write the token in plain text so it can be loaded back
        data = {
            "bot_token": credentials.bot_token.get_secret_value(),
            "bot_username": credentials.bot_username,
        }
        creds_path.write_text(json.dumps(data, indent=2))


def has_agent_bot_credentials(data_dir: Path, agent_id: AgentId) -> bool:
    """Check whether bot credentials exist for the given agent."""
    return _bot_credentials_path(data_dir, agent_id).exists()
