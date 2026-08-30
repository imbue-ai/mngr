"""Tests for the create form's "enable web access" post-create hook."""

from pathlib import Path

import pytest
from pydantic import Field

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import SucceedingCreateShareCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import make_share_probe_result
from imbue.minds.desktop_client.imbue_cloud_cli import ActiveShareCache
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import enable_web_access_for_workspace
from imbue.minds.desktop_client.testing import read_injected_share_env_text
from imbue.minds.desktop_client.workspace_create import WebAccessEnabler
from imbue.minds.primitives import ServiceName
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_AGENT_ID = AgentId("agent-" + "a" * 32)
_HOST_ID = "host-" + "b" * 32


def _client_env_config() -> ClientEnvConfig:
    return ClientEnvConfig(connector_url=FAKE_CONNECTOR_URL, litellm_proxy_url=FAKE_CONNECTOR_URL)


def _empty_resolver() -> StaticBackendResolver:
    return StaticBackendResolver(url_by_agent_and_service={})


class _LabelledResolver(StaticBackendResolver):
    """Static resolver carrying the service origin labels the entry-label resolution reads."""

    labels_by_agent_id: dict[str, dict[str, str]] = Field(default_factory=dict, frozen=True)

    def list_service_labels_for_agent(self, agent_id: AgentId) -> dict[ServiceName, str]:
        labels = self.labels_by_agent_id.get(str(agent_id), {})
        return {ServiceName(name): label for name, label in labels.items()}


class _RecordingCreateShareCli(FakeImbueCloudCli):
    """Records ``create_share`` calls, then fails so the flow stops before materials injection.

    The raise keeps each test at the seam under test -- what the connector
    create was asked -- instead of continuing into share-env rendering and
    injection, which the ``SucceedingCreateShareCli`` tests cover.
    """

    create_share_calls: list[tuple[str, str, str | None]] = Field(
        default_factory=list, description="(account email, host id, entry label) for every create_share call, in order"
    )

    def create_share(
        self,
        *,
        account: str,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareCliInfo:
        self.create_share_calls.append((account, host_id, entry_label))
        raise ImbueCloudCliError("recorded; stopping the bring-up here")


def _store_with_associated_workspace(
    tmp_path: Path, cli: FakeImbueCloudCli | None = None, is_cloud_row: bool = True
) -> tuple[MultiAccountSessionStore, FakeImbueCloudCli]:
    effective_cli = cli if cli is not None else make_fake_imbue_cloud_cli()
    # The enable flow's first step is the one-exec workspace state probe; a
    # healthy answer (gateway present, nothing shared yet) lets each test reach
    # the step it actually exercises.
    caller = effective_cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    effective_cli.add_account(user_id="user-1", email="owner@example.com", display_name=None)
    store = make_session_store_for_test(tmp_path, cli=effective_cli)
    store.associate_created_workspace(
        user_id="user-1",
        agent_id=str(_AGENT_ID),
        host_id=_HOST_ID,
        display_name="ws",
        color=None,
        is_cloud_row=is_cloud_row,
    )
    return store, effective_cli


def test_enable_web_access_cloud_rows_use_the_client_side_share_create(tmp_path: Path) -> None:
    # Cloud rows take the same client-side path as local ones: connector
    # ``shares create`` with the owner as sole grantee, no server-side
    # primitive, and no relay preference (the desktop's latency says nothing
    # about the pool host's). The raise from the recording create also proves
    # a connector failure surfaces as SharingError.
    recording_cli = _RecordingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    store, cli = _store_with_associated_workspace(tmp_path, cli=recording_cli, is_cloud_row=True)
    assert isinstance(cli, _RecordingCreateShareCli)

    with pytest.raises(SharingError):
        enable_web_access_for_workspace(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            is_cloud_row=True,
            cli=cli,
            session_store=store,
            backend_resolver=_empty_resolver(),
            client_env_config=_client_env_config(),
        )

    assert cli.create_share_calls == [("owner@example.com", _HOST_ID, None)]


def test_enable_web_access_raises_for_a_workspace_with_no_account(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    store = make_session_store_for_test(tmp_path, cli=cli)

    with pytest.raises(SharingError):
        enable_web_access_for_workspace(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            is_cloud_row=True,
            cli=cli,
            session_store=store,
            backend_resolver=_empty_resolver(),
            client_env_config=_client_env_config(),
        )
    assert cli.shares_by_account == {}


def test_enable_web_access_local_rows_record_the_shell_label(tmp_path: Path) -> None:
    # Local docker/lima rows go through the client-side share create; the
    # shell service's origin label must ride along, because the chrome can
    # only enter the workspace at <label>.<domain> (the bare domain is
    # unrouted on the relay).
    recording_cli = _RecordingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    store, cli = _store_with_associated_workspace(tmp_path, cli=recording_cli, is_cloud_row=False)
    assert isinstance(cli, _RecordingCreateShareCli)
    resolver = _LabelledResolver(
        url_by_agent_and_service={},
        labels_by_agent_id={str(_AGENT_ID): {"system_interface": "system_interface-abc123"}},
    )

    with pytest.raises(SharingError):
        enable_web_access_for_workspace(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            is_cloud_row=False,
            cli=cli,
            session_store=store,
            backend_resolver=resolver,
            client_env_config=_client_env_config(),
        )

    assert cli.create_share_calls == [("owner@example.com", _HOST_ID, "system_interface-abc123")]


def test_enable_web_access_local_row_resolves_urls_without_an_app_context(tmp_path: Path) -> None:
    # Regression: the post-create enabler runs in a worker thread with no Flask
    # app context, so the connector/broker URLs must come from the threaded
    # ClientEnvConfig, NOT get_state()/current_app. This test drives the local
    # bring-up to completion with no app context pushed; before the fix it
    # raised "RuntimeError: Working outside of application context" at the
    # share-env rendering step (so the container never got share.env at all).
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    store, cli = _store_with_associated_workspace(tmp_path, cli=cli, is_cloud_row=False)
    resolver = _LabelledResolver(
        url_by_agent_and_service={},
        labels_by_agent_id={str(_AGENT_ID): {"system_interface": "system_interface-abc123"}},
    )

    enable_web_access_for_workspace(
        agent_id=_AGENT_ID,
        host_id=_HOST_ID,
        is_cloud_row=False,
        cli=cli,
        session_store=store,
        backend_resolver=resolver,
        client_env_config=_client_env_config(),
    )

    # The share.env actually reached the agent, carrying the connector URL
    # from the threaded config (proving the URL resolution ran off-context).
    share_env_text = read_injected_share_env_text(cli)
    connector_url = str(FAKE_CONNECTOR_URL).rstrip("/")
    assert f"SHARE_CONNECTOR_URL={connector_url}" in share_env_text
    assert f"SHARE_CHROME_ORIGIN={connector_url}" in share_env_text


def test_web_access_enabler_swallows_sharing_failures(tmp_path: Path) -> None:
    # A share bring-up hiccup must never flip an already-successful create:
    # the agent creator marks the whole create FAILED on any raised exception.
    recording_cli = _RecordingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    store, cli = _store_with_associated_workspace(tmp_path, cli=recording_cli, is_cloud_row=True)
    assert isinstance(cli, _RecordingCreateShareCli)
    enabler = WebAccessEnabler(
        cli=cli,
        session_store=store,
        is_cloud_row=True,
        backend_resolver=_empty_resolver(),
        client_env_config=_client_env_config(),
        active_share_cache=ActiveShareCache(),
    )

    enabler(_AGENT_ID, HostId(_HOST_ID))

    # The bring-up was attempted (and failed inside create_share) without the
    # failure escaping the enabler.
    assert cli.create_share_calls == [("owner@example.com", _HOST_ID, None)]
    assert cli.shares_by_account == {}


def test_web_access_enabler_enables_sharing_for_cloud_rows(tmp_path: Path) -> None:
    # The full cloud-row bring-up runs client-side: the connector share is
    # created and the share.env materials land in the agent via mngr exec.
    succeeding_cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    store, cli = _store_with_associated_workspace(tmp_path, cli=succeeding_cli, is_cloud_row=True)
    enabler = WebAccessEnabler(
        cli=cli,
        session_store=store,
        is_cloud_row=True,
        backend_resolver=_empty_resolver(),
        client_env_config=_client_env_config(),
        active_share_cache=ActiveShareCache(),
    )

    enabler(_AGENT_ID, HostId(_HOST_ID))

    assert cli.shares_by_account.get("owner@example.com", {}).get(_HOST_ID) == "active"
    share_env_text = read_injected_share_env_text(cli)
    connector_url = str(FAKE_CONNECTOR_URL).rstrip("/")
    assert f"SHARE_CONNECTOR_URL={connector_url}" in share_env_text


def test_web_access_enabler_invalidates_the_readiness_polls_share_cache(tmp_path: Path) -> None:
    # A readiness poll racing the post-create worker can cache a "not shared"
    # lookup just before the enable lands; the enabler must drop that entry
    # (like the sharing PUT handler does) so the poll observes the new share
    # immediately rather than at cache-TTL expiry.
    succeeding_cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    store, cli = _store_with_associated_workspace(tmp_path, cli=succeeding_cli, is_cloud_row=True)
    cache = ActiveShareCache()
    cache.put(_HOST_ID, None)
    enabler = WebAccessEnabler(
        cli=cli,
        session_store=store,
        is_cloud_row=True,
        backend_resolver=_empty_resolver(),
        client_env_config=_client_env_config(),
        active_share_cache=cache,
    )

    enabler(_AGENT_ID, HostId(_HOST_ID))

    assert cache.get(_HOST_ID) is None
