from pathlib import Path

from pydantic import SecretStr

from imbue.minds.telegram.credential_store import has_agent_bot_credentials
from imbue.minds.telegram.credential_store import load_agent_bot_credentials
from imbue.minds.telegram.credential_store import load_telegram_user_credentials
from imbue.minds.telegram.credential_store import save_agent_bot_credentials
from imbue.minds.telegram.credential_store import save_telegram_user_credentials
from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.data_types import TelegramUserCredentials
from imbue.mngr.primitives import AgentId


def test_user_credentials_round_trip_through_store(tmp_path: Path) -> None:
    data_dir = tmp_path
    creds = TelegramUserCredentials(
        dc_id=3,
        auth_key_hex="ab" * 256,
        user_id="99887766",
        first_name="TestUser",
    )

    assert load_telegram_user_credentials(data_dir) is None

    save_telegram_user_credentials(data_dir, creds)
    loaded = load_telegram_user_credentials(data_dir)

    assert loaded is not None
    assert loaded.dc_id == 3
    assert loaded.auth_key_hex == "ab" * 256
    assert loaded.user_id == "99887766"
    assert loaded.first_name == "TestUser"


def test_bot_credentials_round_trip_through_store(tmp_path: Path) -> None:
    data_dir = tmp_path
    agent_id = AgentId()
    creds = TelegramBotCredentials(
        bot_token=SecretStr("123456:ABC-DEF-secret-token"),
        bot_username="test_mind_bot",
    )

    assert not has_agent_bot_credentials(data_dir, agent_id)
    assert load_agent_bot_credentials(data_dir, agent_id) is None

    save_agent_bot_credentials(data_dir, agent_id, creds)

    assert has_agent_bot_credentials(data_dir, agent_id)
    loaded = load_agent_bot_credentials(data_dir, agent_id)

    assert loaded is not None
    assert loaded.bot_token.get_secret_value() == "123456:ABC-DEF-secret-token"
    assert loaded.bot_username == "test_mind_bot"


def test_load_user_credentials_returns_none_for_corrupted_file(tmp_path: Path) -> None:
    data_dir = tmp_path
    creds_path = data_dir / "telegram" / "user_credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text("not valid json {{{")

    assert load_telegram_user_credentials(data_dir) is None


def test_load_user_credentials_returns_none_for_invalid_schema(tmp_path: Path) -> None:
    data_dir = tmp_path
    creds_path = data_dir / "telegram" / "user_credentials.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but missing required fields (dc_id, auth_key_hex, user_id, first_name)
    creds_path.write_text('{"unexpected_field": "value"}')

    assert load_telegram_user_credentials(data_dir) is None


def test_load_bot_credentials_returns_none_for_invalid_schema(tmp_path: Path) -> None:
    data_dir = tmp_path
    agent_id = AgentId()
    creds_path = data_dir / "telegram" / "bots" / f"{agent_id}.json"
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but missing required fields (bot_token, bot_username)
    creds_path.write_text('{"unexpected_field": "value"}')

    assert load_agent_bot_credentials(data_dir, agent_id) is None


def test_multiple_agents_have_independent_bot_credentials(tmp_path: Path) -> None:
    data_dir = tmp_path
    agent_1 = AgentId()
    agent_2 = AgentId()

    creds_1 = TelegramBotCredentials(
        bot_token=SecretStr("token_1"),
        bot_username="bot_one",
    )
    creds_2 = TelegramBotCredentials(
        bot_token=SecretStr("token_2"),
        bot_username="bot_two",
    )

    save_agent_bot_credentials(data_dir, agent_1, creds_1)
    save_agent_bot_credentials(data_dir, agent_2, creds_2)

    loaded_1 = load_agent_bot_credentials(data_dir, agent_1)
    loaded_2 = load_agent_bot_credentials(data_dir, agent_2)

    assert loaded_1 is not None
    assert loaded_1.bot_username == "bot_one"
    assert loaded_2 is not None
    assert loaded_2.bot_username == "bot_two"
