from pydantic import SecretStr

from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.data_types import TelegramUserCredentials


def test_telegram_user_credentials_round_trips_through_json() -> None:
    creds = TelegramUserCredentials(
        dc_id=3,
        auth_key_hex="ab" * 256,
        user_id="12345",
        first_name="Alice",
    )
    raw = creds.model_dump_json()
    restored = TelegramUserCredentials.model_validate_json(raw)
    assert restored.dc_id == 3
    assert restored.auth_key_hex == "ab" * 256
    assert restored.user_id == "12345"
    assert restored.first_name == "Alice"


def test_telegram_bot_credentials_hides_token_in_repr() -> None:
    creds = TelegramBotCredentials(
        bot_token=SecretStr("123456:ABC-DEF"),
        bot_username="test_bot",
    )
    repr_str = repr(creds)
    assert "123456:ABC-DEF" not in repr_str
    assert creds.bot_token.get_secret_value() == "123456:ABC-DEF"
    assert creds.bot_username == "test_bot"
