from pathlib import Path

from inline_snapshot import snapshot
from pydantic import SecretStr

from imbue.minds.config.data_types import MindPaths
from imbue.minds.telegram.credential_store import save_agent_bot_credentials
from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.minds.telegram.setup import generate_bot_display_name
from imbue.minds.telegram.setup import generate_bot_username
from imbue.mngr.primitives import AgentId


def test_generate_bot_username_from_simple_name() -> None:
    assert generate_bot_username("selene") == snapshot("selene_bot")


def test_generate_bot_username_sanitizes_special_characters() -> None:
    assert generate_bot_username("My Cool Agent!") == snapshot("my_cool_agent_bot")


def test_generate_bot_username_handles_empty_name() -> None:
    assert generate_bot_username("") == snapshot("mind_bot")


def test_generate_bot_username_truncates_long_names() -> None:
    result = generate_bot_username("a" * 50)
    assert len(result) <= 32
    assert result.endswith("_bot")


def test_generate_bot_username_pads_short_names() -> None:
    result = generate_bot_username("ab")
    assert len(result) >= 5
    assert result.endswith("_bot")


def test_generate_bot_display_name() -> None:
    assert generate_bot_display_name("selene") == snapshot("selene Bot")


def test_orchestrator_start_setup_returns_done_when_already_configured(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    orchestrator = TelegramSetupOrchestrator(paths=paths)
    agent_id = AgentId()

    # Pre-populate bot credentials
    save_agent_bot_credentials(
        data_dir=tmp_path,
        agent_id=agent_id,
        credentials=TelegramBotCredentials(
            bot_token=SecretStr("existing-token"),
            bot_username="existing_bot",
        ),
    )

    orchestrator.start_setup(agent_id=agent_id, agent_name="test")
    info = orchestrator.get_setup_info(agent_id)

    assert info is not None
    assert info.status == TelegramSetupStatus.DONE
    assert info.bot_username == "existing_bot"


def test_orchestrator_agent_has_telegram_returns_false_when_no_credentials(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    orchestrator = TelegramSetupOrchestrator(paths=paths)
    agent_id = AgentId()

    assert not orchestrator.agent_has_telegram(agent_id)


def test_orchestrator_agent_has_telegram_returns_true_when_credentials_exist(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    orchestrator = TelegramSetupOrchestrator(paths=paths)
    agent_id = AgentId()

    save_agent_bot_credentials(
        data_dir=tmp_path,
        agent_id=agent_id,
        credentials=TelegramBotCredentials(
            bot_token=SecretStr("token"),
            bot_username="bot",
        ),
    )

    assert orchestrator.agent_has_telegram(agent_id)


def test_orchestrator_get_setup_info_returns_none_for_unknown_agent(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    orchestrator = TelegramSetupOrchestrator(paths=paths)

    assert orchestrator.get_setup_info(AgentId()) is None


def test_orchestrator_wait_for_all_returns_immediately_when_no_threads(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    orchestrator = TelegramSetupOrchestrator(paths=paths)

    orchestrator.wait_for_all(timeout=0.1)
