"""Integration tests for the Telegram setup endpoints in the desktop client."""

from pathlib import Path

from pydantic import SecretStr
from starlette.testclient import TestClient

from imbue.minds.config.data_types import MindPaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_server_log
from imbue.minds.primitives import OneTimeCode
from imbue.minds.telegram.credential_store import save_agent_bot_credentials
from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.mngr.primitives import AgentId


def _create_test_server_with_telegram(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, TelegramSetupOrchestrator, AgentId]:
    """Create a desktop client with telegram support and a test agent."""
    agent_id = AgentId()
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    agents_json = make_agents_json(agent_id)
    server_log = make_server_log("web", "http://test-backend")
    resolver = make_resolver_with_data(
        agents_json=agents_json,
        server_logs={str(agent_id): server_log},
    )

    paths = MindPaths(data_dir=tmp_path / "minds_data")
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        telegram_orchestrator=telegram_orchestrator,
    )
    client = TestClient(app)
    return client, auth_store, telegram_orchestrator, agent_id


def _authenticate(client: TestClient, auth_store: FileAuthStore) -> None:
    """Authenticate the test client."""
    code = OneTimeCode("test-auth-code-telegram-endpoints")
    auth_store.add_one_time_code(code=code)
    response = client.get(f"/authenticate?one_time_code={code}", follow_redirects=False)
    assert response.status_code == 307
    # The response sets a session cookie -- subsequent requests use it automatically


def test_telegram_status_returns_404_when_no_setup_started(tmp_path: Path) -> None:
    client, auth_store, _, agent_id = _create_test_server_with_telegram(tmp_path)
    _authenticate(client, auth_store)

    response = client.get(f"/api/agents/{agent_id}/telegram/status")
    assert response.status_code == 404


def test_telegram_status_returns_done_when_bot_credentials_exist(tmp_path: Path) -> None:
    client, auth_store, orchestrator, agent_id = _create_test_server_with_telegram(tmp_path)
    _authenticate(client, auth_store)

    # Pre-populate bot credentials
    save_agent_bot_credentials(
        data_dir=orchestrator.paths.data_dir,
        agent_id=agent_id,
        credentials=TelegramBotCredentials(
            bot_token=SecretStr("fake-token"),
            bot_username="test_bot",
        ),
    )

    response = client.get(f"/api/agents/{agent_id}/telegram/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "DONE"


def test_telegram_setup_requires_authentication(tmp_path: Path) -> None:
    client, _, _, agent_id = _create_test_server_with_telegram(tmp_path)

    response = client.post(f"/api/agents/{agent_id}/telegram/setup")
    assert response.status_code == 403


def test_telegram_status_requires_authentication(tmp_path: Path) -> None:
    client, _, _, agent_id = _create_test_server_with_telegram(tmp_path)

    response = client.get(f"/api/agents/{agent_id}/telegram/status")
    assert response.status_code == 403


def test_telegram_setup_returns_done_immediately_when_already_configured(tmp_path: Path) -> None:
    client, auth_store, orchestrator, agent_id = _create_test_server_with_telegram(tmp_path)
    _authenticate(client, auth_store)

    # Pre-populate bot credentials
    save_agent_bot_credentials(
        data_dir=orchestrator.paths.data_dir,
        agent_id=agent_id,
        credentials=TelegramBotCredentials(
            bot_token=SecretStr("existing-token"),
            bot_username="existing_bot",
        ),
    )

    response = client.post(f"/api/agents/{agent_id}/telegram/setup")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "CHECKING_CREDENTIALS"

    # Status should show DONE immediately since credentials exist
    status_response = client.get(f"/api/agents/{agent_id}/telegram/status")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "DONE"


def _create_test_server_with_two_agents(
    tmp_path: Path,
) -> tuple[TestClient, FileAuthStore, TelegramSetupOrchestrator, AgentId, AgentId]:
    """Create a desktop client with two agents so the landing page shows a list."""
    agent_id_1 = AgentId()
    agent_id_2 = AgentId()
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)

    agents_json = make_agents_json(agent_id_1, agent_id_2)
    resolver = make_resolver_with_data(
        agents_json=agents_json,
        server_logs={
            str(agent_id_1): make_server_log("web", "http://test-backend-1"),
            str(agent_id_2): make_server_log("web", "http://test-backend-2"),
        },
    )

    paths = MindPaths(data_dir=tmp_path / "minds_data")
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        telegram_orchestrator=telegram_orchestrator,
    )
    client = TestClient(app)
    return client, auth_store, telegram_orchestrator, agent_id_1, agent_id_2


def test_landing_page_shows_telegram_buttons_when_orchestrator_configured(tmp_path: Path) -> None:
    client, auth_store, _, agent_id_1, agent_id_2 = _create_test_server_with_two_agents(tmp_path)
    _authenticate(client, auth_store)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Setup Telegram" in response.text


def test_landing_page_shows_telegram_active_when_bot_exists(tmp_path: Path) -> None:
    client, auth_store, orchestrator, agent_id_1, agent_id_2 = _create_test_server_with_two_agents(tmp_path)
    _authenticate(client, auth_store)

    # Pre-populate bot credentials for one agent
    save_agent_bot_credentials(
        data_dir=orchestrator.paths.data_dir,
        agent_id=agent_id_1,
        credentials=TelegramBotCredentials(
            bot_token=SecretStr("active-token"),
            bot_username="active_bot",
        ),
    )

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Telegram active" in response.text
    # The other agent should still show the setup button
    assert "Setup Telegram" in response.text
