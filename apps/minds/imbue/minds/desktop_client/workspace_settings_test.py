"""Unit tests for the account associate/disassociate helpers in ``workspace_settings``.

Focused on the share-teardown side of disassociation: an active machine share
must not outlive the account association, even when discovery no longer
reports the workspace (a stopped host is undiscovered but its workspace record
still carries the ``host-<hex>`` coordinate).
"""

from pathlib import Path

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.workspace_settings import disassociate_workspace_account
from imbue.mngr.primitives import AgentId

_AGENT = AgentId("agent-" + "a" * 32)
_HOST = "host-" + "a" * 32
_USER_ID = "user-1"
_EMAIL = "owner@example.com"


def test_disassociate_tears_down_share_for_stopped_undiscovered_workspace(tmp_path: Path) -> None:
    """Discovery does not know the agent, but the record's host id still revokes the share."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        _USER_ID, str(_AGENT), _HOST, display_name="ws", color=None, is_cloud_row=False
    )
    cli.add_share(_EMAIL, _HOST)
    undiscovering_resolver = StaticBackendResolver(url_by_agent_and_service={})

    disassociate_workspace_account(_AGENT, undiscovering_resolver, session_store, cli)

    assert cli.deleted_share_host_ids == [_HOST]
    assert session_store.get_account_for_workspace(str(_AGENT)) is None


def test_disassociate_without_share_only_removes_association(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        _USER_ID, str(_AGENT), _HOST, display_name="ws", color=None, is_cloud_row=False
    )

    disassociate_workspace_account(_AGENT, StaticBackendResolver(url_by_agent_and_service={}), session_store, cli)

    assert cli.deleted_share_host_ids == []
    assert session_store.get_account_for_workspace(str(_AGENT)) is None
