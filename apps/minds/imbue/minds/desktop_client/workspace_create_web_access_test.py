"""Tests for the create form's "enable web access" post-create hook."""

import base64
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import SecretStr

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import enable_web_access_for_workspace
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

    The local-row bring-up continues past ``create_share`` into share-env
    rendering, which reads app state this unit test does not assemble; the
    raise keeps the test at the seam under test (what the connector create
    was asked to record).
    """

    create_share_calls: list[tuple[str, str, str | None]] = Field(
        default_factory=list, description="(account email, host id, entry label) for every create_share call, in order"
    )

    def create_share(self, *, account: str, host_id: str, entry_label: str | None = None) -> ShareCliInfo:
        self.create_share_calls.append((account, host_id, entry_label))
        raise ImbueCloudCliError("recorded; stopping the bring-up here")


def _store_with_associated_workspace(
    tmp_path: Path, cli: FakeImbueCloudCli | None = None, is_cloud_row: bool = True
) -> tuple[MultiAccountSessionStore, FakeImbueCloudCli]:
    effective_cli = cli if cli is not None else make_fake_imbue_cloud_cli()
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


def test_enable_web_access_delegates_cloud_rows_to_the_connector_primitive(tmp_path: Path) -> None:
    store, cli = _store_with_associated_workspace(tmp_path)

    enable_web_access_for_workspace(
        agent_id=_AGENT_ID,
        host_id=_HOST_ID,
        is_cloud_row=True,
        cli=cli,
        session_store=store,
        backend_resolver=_empty_resolver(),
        client_env_config=_client_env_config(),
    )

    assert cli.web_access_calls == [("owner@example.com", _HOST_ID)]


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
    assert cli.web_access_calls == []


def test_enable_web_access_wraps_connector_failures_as_sharing_errors(tmp_path: Path) -> None:
    store, cli = _store_with_associated_workspace(tmp_path)
    cli.web_access_error_to_raise = ImbueCloudCliError("connector down")

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
    assert cli.web_access_calls == []


class _SucceedingCreateShareCli(FakeImbueCloudCli):
    """A local-row share create that returns real relay coordinates.

    Lets the local bring-up run all the way through share-env rendering and
    materials injection (the default ``RecordingMngrCaller`` records the exec
    writes), so a test can assert the whole off-request-context path.
    """

    def create_share(self, *, account: str, host_id: str, entry_label: str | None = None) -> ShareCliInfo:
        return ShareCliInfo(
            host_id=host_id,
            workspace_domain=f"{host_id}.owner1234.us1.shares.example",
            region="us1",
            state="active",
            relay_endpoint="relay-us1.shares.example:7000",
            relay_token=SecretStr("relay-token-xyz"),
        )


def _injected_share_env_text(cli: FakeImbueCloudCli) -> str:
    """Decode the share.env body the (recorded) ``mngr exec`` write shipped into the agent."""
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    for argv in caller.calls:
        command = argv[-1]
        if "data/.secrets/share.env" in command and "printf '%s'" in command:
            encoded = command.split("printf '%s' ", 1)[1].split(" | base64 -d", 1)[0].strip()
            return base64.b64decode(encoded).decode("utf-8")
    raise AssertionError("no share.env write was recorded")


def test_enable_web_access_local_row_resolves_urls_without_an_app_context(tmp_path: Path) -> None:
    # Regression: the post-create enabler runs in a worker thread with no Flask
    # app context, so the connector/broker URLs must come from the threaded
    # ClientEnvConfig, NOT get_state()/current_app. This test drives the local
    # bring-up to completion with no app context pushed; before the fix it
    # raised "RuntimeError: Working outside of application context" at the
    # share-env rendering step (so the container never got share.env at all).
    cli = _SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
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
    share_env_text = _injected_share_env_text(cli)
    connector_url = str(FAKE_CONNECTOR_URL).rstrip("/")
    assert f"SHARE_CONNECTOR_URL={connector_url}" in share_env_text
    assert f"SHARE_CHROME_ORIGIN={connector_url}" in share_env_text


def test_web_access_enabler_swallows_sharing_failures(tmp_path: Path) -> None:
    # A share bring-up hiccup must never flip an already-successful create:
    # the agent creator marks the whole create FAILED on any raised exception.
    store, cli = _store_with_associated_workspace(tmp_path)
    cli.web_access_error_to_raise = ImbueCloudCliError("connector down")
    enabler = WebAccessEnabler(
        cli=cli,
        session_store=store,
        is_cloud_row=True,
        backend_resolver=_empty_resolver(),
        client_env_config=_client_env_config(),
    )

    enabler(_AGENT_ID, HostId(_HOST_ID))

    assert cli.web_access_calls == []


def test_web_access_enabler_enables_sharing_for_cloud_rows(tmp_path: Path) -> None:
    store, cli = _store_with_associated_workspace(tmp_path)
    enabler = WebAccessEnabler(
        cli=cli,
        session_store=store,
        is_cloud_row=True,
        backend_resolver=_empty_resolver(),
        client_env_config=_client_env_config(),
    )

    enabler(_AGENT_ID, HostId(_HOST_ID))

    assert cli.web_access_calls == [("owner@example.com", _HOST_ID)]
