from typing import Final

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel

TELEGRAM_WEB_URL: Final[str] = "https://web.telegram.org/a/"


class TelegramUserCredentials(FrozenModel):
    """MTProto user credentials extracted from web.telegram.org localStorage."""

    dc_id: int = Field(description="Telegram data center ID (1-5)")
    auth_key_hex: str = Field(description="MTProto auth key as a 512-character hex string (256 bytes)")
    user_id: str = Field(description="Telegram numeric user ID")
    first_name: str = Field(description="User's first name from their Telegram account")


class TelegramBotCredentials(FrozenModel):
    """Credentials for a Telegram bot created via BotFather."""

    bot_token: SecretStr = Field(description="Bot API token from BotFather (format: id:hash)")
    bot_username: str = Field(description="Bot username (ending in 'bot')")
