"""Unit tests for the account associate/disassociate helpers in ``workspace_settings``.

Focused on the share-teardown side of disassociation: an active machine share
must not outlive the account association, even when discovery no longer
reports the workspace (a stopped host is undiscovered but its workspace record
still carries the ``host-<hex>`` coordinate).
"""

from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.workspace_color_writes import WorkspaceColorWrites
from imbue.minds.desktop_client.workspace_settings import disassociate_workspace_account
from imbue.minds.desktop_client.workspace_settings import set_workspace_color
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
