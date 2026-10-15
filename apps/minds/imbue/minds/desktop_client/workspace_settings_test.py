"""Unit tests for the account associate/disassociate helpers in ``workspace_settings``.

Focused on the share-teardown side of disassociation: an active machine share
must not outlive the account association, even when discovery no longer
reports the workspace (a stopped host is undiscovered but its workspace record
still carries the ``host-<hex>`` coordinate).
"""

from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.workspace_color_writes import WorkspaceColorWrites
from imbue.minds.desktop_client.workspace_settings import WorkspaceAssociationError
from imbue.minds.desktop_client.workspace_settings import associate_workspace_account
from imbue.minds.desktop_client.workspace_settings import disassociate_workspace_account
from imbue.minds.desktop_client.workspace_settings import set_workspace_color
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_until

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

    disassociate_workspace_account(_AGENT, undiscovering_resolver, session_store, None, cli)

    assert cli.deleted_share_host_ids == [_HOST]
    assert session_store.get_account_for_workspace(str(_AGENT)) is None


def test_disassociate_without_share_only_removes_association(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        _USER_ID, str(_AGENT), _HOST, display_name="ws", color=None, is_cloud_row=False
    )

    disassociate_workspace_account(
        _AGENT, StaticBackendResolver(url_by_agent_and_service={}), session_store, None, cli
    )

    assert cli.deleted_share_host_ids == []
    assert session_store.get_account_for_workspace(str(_AGENT)) is None


class _ConnectionHostedResolver(StaticBackendResolver):
    """Static resolver reporting every known agent as living on one connection instance."""

    provider_name: str

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=_HOST, provider_name=self.provider_name)


def test_account_link_is_refused_on_an_account_owned_connection_but_not_on_a_local_profile_one(
    tmp_path: Path,
) -> None:
    """An account-owned connection binds its workspaces to the account for life; a device-only connection does not."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    connection_store = make_connection_store_for_test(tmp_path / "minds", cli)
    unlock_account_with_master_password_for_test(connection_store.paths, _USER_ID)
    account_connection_id = add_modal_connection_for_test(connection_store, _USER_ID, "team modal")
    local_connection_id = add_modal_connection_for_test(connection_store, LOCAL_PROFILE_USER_ID, "my modal")
    on_account_connection = _ConnectionHostedResolver(
        url_by_agent_and_service={},
        provider_name=provider_instance_name_for_connection(ConnectionBackend.MODAL, account_connection_id),
    )
    on_local_connection = _ConnectionHostedResolver(
        url_by_agent_and_service={},
        provider_name=provider_instance_name_for_connection(ConnectionBackend.MODAL, local_connection_id),
    )

    with pytest.raises(WorkspaceAssociationError) as associate_refusal:
        associate_workspace_account(_AGENT, _USER_ID, on_account_connection, session_store, connection_store)
    with pytest.raises(WorkspaceAssociationError) as disassociate_refusal:
        disassociate_workspace_account(_AGENT, on_account_connection, session_store, connection_store, cli)
    assert (associate_refusal.value.status_code, disassociate_refusal.value.status_code) == (403, 403)
    assert session_store.get_account_for_workspace(str(_AGENT)) is None

    matched = associate_workspace_account(_AGENT, _USER_ID, on_local_connection, session_store, connection_store)
    assert matched.user_id == _USER_ID
    assert session_store.get_account_for_workspace(str(_AGENT)) is not None
    disassociate_workspace_account(_AGENT, on_local_connection, session_store, connection_store, cli)
    assert session_store.get_account_for_workspace(str(_AGENT)) is None


def test_disassociate_clears_the_grants_of_an_unpublished_workspace(tmp_path: Path) -> None:
    # Unpublishing keeps the grants document, so an unlink has to take it.
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        _USER_ID, str(_AGENT), _HOST, display_name="ws", color=None, is_cloud_row=False
    )
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)

    disassociate_workspace_account(
        _AGENT, StaticBackendResolver(url_by_agent_and_service={}), session_store, None, cli
    )

    # No share to delete, but the document goes anyway.
    assert cli.deleted_share_host_ids == []
    clear_commands = [call[2] for call in caller.calls if call and call[0] == "exec" and "rm -f" in call[2]]
    assert len(clear_commands) == 1
    assert "data/.secrets/share_grants.toml" in clear_commands[0]
    assert "data/.secrets/share.env" in clear_commands[0]


def test_a_color_pick_replaced_while_waiting_for_its_turn_is_shown_but_never_written(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    mngr_calls = tmp_path / "mngr-calls.txt"
    fake_mngr = tmp_path / "bin" / "mngr"
    fake_mngr.parent.mkdir()
    fake_mngr.write_text(f'#!/bin/sh\necho "$@" >> {mngr_calls}\n')
    fake_mngr.chmod(0o755)
    writes = WorkspaceColorWrites()

    with ConcurrencyGroup(name="test-superseded-color-pick") as concurrency_group:
        with writes.turn_to_write(agent_id):
            concurrency_group.start_new_thread(
                target=set_workspace_color,
                args=(
                    agent_id,
                    "#111111",
                    resolver,
                    str(fake_mngr),
                    tmp_path / "mngr",
                    root_concurrency_group,
                    writes,
                ),
                name="test-color-pick",
            )
            assert poll_until(lambda: resolver.get_workspace_color(agent_id) == "#111111", timeout=5.0)
            writes.register_pick(agent_id)

    assert not mngr_calls.exists()
