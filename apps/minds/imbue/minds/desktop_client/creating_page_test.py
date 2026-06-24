"""Flask test-client coverage for the ``GET /creating/<id>`` page handler.

The creating-page route renders an in-flight workspace creation. The creation
registry is in-memory, so a ``/creating/<id>`` window that outlives its creation
-- reopened after an app restart, or after a failed creation was cleaned up --
finds no info for its id. That full-page navigation must fall back to the
landing page rather than stranding the window on a bare ``404 Unknown agent
creation``.
"""

from pathlib import Path

from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreationId


def _make_authenticated_client(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    agent_creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=MngrCliBackendResolver(),
        http_client=None,
        agent_creator=agent_creator,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def test_creating_page_redirects_to_landing_for_unknown_creation(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """An unknown creation id falls back to the landing page instead of a 404."""
    client = _make_authenticated_client(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.get(f"/creating/{CreationId()}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["Location"] == "/"
