import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import Field

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.api_models import SharingGrantList
from imbue.minds.desktop_client.api_models import SharingGrantsDocument
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import SucceedingCreateShareCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import make_share_probe_result
from imbue.minds.desktop_client.forward_identity import ForwardHeadersFile
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.identity_records import IdentityCache
from imbue.minds.desktop_client.identity_records import IdentityRecord
from imbue.minds.desktop_client.identity_records import now_utc
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.imbue_cloud_cli import UserIdentityCliInfo
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.sharing_handler import GrantsRefusedError
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import _parse_grants_toml
from imbue.minds.desktop_client.sharing_handler import _publish_workspace_with_cli
from imbue.minds.desktop_client.sharing_handler import _resolve_grant_identities
from imbue.minds.desktop_client.sharing_handler import _save_grants_with_cli
from imbue.minds.desktop_client.sharing_handler import describe_connector_failure
from imbue.minds.desktop_client.sharing_handler import get_sharing
from imbue.minds.desktop_client.sharing_handler import migrate_stale_share
from imbue.minds.desktop_client.sharing_handler import pick_lowest_latency_relay_region
from imbue.minds.desktop_client.sharing_handler import probe_share_readiness
from imbue.minds.desktop_client.sharing_handler import resolve_agent_for_host
from imbue.minds.desktop_client.sharing_handler import split_relay_endpoint
from imbue.minds.desktop_client.sharing_handler import unpublish_workspace
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.testing import read_injected_share_env_text
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.shares.example"


def _client_env_config() -> ClientEnvConfig:
    return ClientEnvConfig(connector_url=FAKE_CONNECTOR_URL, litellm_proxy_url=FAKE_CONNECTOR_URL)


def test_probe_share_readiness_true_on_any_http_response() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{_DOMAIN}/"
        return httpx.Response(302, headers={"location": "https://accounts.example/share/authorize?x=1"})

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is True


def test_probe_share_readiness_true_even_on_403() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is True


def test_probe_share_readiness_false_on_transport_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("relay not reachable")

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is False


def test_split_relay_endpoint_handles_hostnames_and_ipv6_literals() -> None:
    assert split_relay_endpoint("relay-us1.example:7000") == ("relay-us1.example", 7000)
    assert split_relay_endpoint("203.0.113.9:7000") == ("203.0.113.9", 7000)
    # Bracketed IPv6 literals unwrap to the bare address.
    assert split_relay_endpoint("[::1]:7000") == ("::1", 7000)
    assert split_relay_endpoint("[2001:db8::2]:7000") == ("2001:db8::2", 7000)
    # Malformed shapes: no port, empty host, non-numeric port, unbracketed
    # IPv6 (ambiguous port split), empty brackets.
    assert split_relay_endpoint("relay-us1.example") is None
    assert split_relay_endpoint(":7000") is None
    assert split_relay_endpoint("relay-us1.example:web") is None
    assert split_relay_endpoint("2001:db8::2:7000") is None
    assert split_relay_endpoint("[]:7000") is None


def test_pick_lowest_latency_relay_region_prefers_the_fastest_reachable_relay() -> None:
    relays = {"us1": ("relay-us1.example:7000",), "us2": ("relay-us2.example:7000",)}
    seconds_by_endpoint = {"relay-us1.example:7000": 0.120, "relay-us2.example:7000": 0.030}

    picked = pick_lowest_latency_relay_region(relays, lambda endpoint: seconds_by_endpoint[endpoint])

    assert picked == "us2"


def test_pick_lowest_latency_relay_region_skips_unreachable_relays() -> None:
    relays = {"us1": ("relay-us1.example:7000",), "us2": ("relay-us2.example:7000",)}
    seconds_by_endpoint: dict[str, float | None] = {
        "relay-us1.example:7000": None,
        "relay-us2.example:7000": 0.500,
    }

    picked = pick_lowest_latency_relay_region(relays, lambda endpoint: seconds_by_endpoint[endpoint])

    assert picked == "us2"


def test_pick_lowest_latency_relay_region_scores_a_region_by_its_best_endpoint() -> None:
    # us1's second relay is unreachable, but its first answers fastest: the
    # region is scored by its best endpoint, so one dead relay never costs a
    # region the pick.
    relays = {
        "us1": ("relay-us1a.example:7000", "relay-us1b.example:7000"),
        "us2": ("relay-us2.example:7000",),
    }
    seconds_by_endpoint: dict[str, float | None] = {
        "relay-us1a.example:7000": 0.020,
        "relay-us1b.example:7000": None,
        "relay-us2.example:7000": 0.100,
    }

    picked = pick_lowest_latency_relay_region(relays, lambda endpoint: seconds_by_endpoint[endpoint])

    assert picked == "us1"


def test_pick_lowest_latency_relay_region_returns_none_when_nothing_answers() -> None:
    relays = {"us1": ("relay-us1.example:7000",), "us2": ("relay-us2.example:7000",)}

    assert pick_lowest_latency_relay_region(relays, lambda endpoint: None) is None


def test_pick_lowest_latency_relay_region_skips_measurement_for_a_single_region() -> None:
    def _must_not_measure(endpoint: str) -> float | None:
        raise AssertionError(f"unexpected measurement of {endpoint}")

    assert pick_lowest_latency_relay_region({"us1": ("relay-us1.example:7000",)}, _must_not_measure) == "us1"
    assert pick_lowest_latency_relay_region({}, _must_not_measure) is None


def test_grants_toml_roundtrips_through_render_and_parse() -> None:
    # ``users`` must survive the round trip verbatim: the gateway writes user
    # ids into it when it upgrades an invite, and a save that dropped them
    # would revoke access.
    workspace_grants = {"users": ["user-1"], "emails": ["bob@example.com"], "email_domains": ["partner.org"]}
    service_grants = {
        "web": {"users": [], "emails": ["carol@example.com"], "email_domains": []},
        "my-app": {"users": ["user-2", "user-3"], "emails": [], "email_domains": ["viewer.dev"]},
    }

    rendered = render_grants_toml(workspace_grants, service_grants)
    parsed = _parse_grants_toml(rendered)

    assert parsed is not None
    parsed_workspace, parsed_services = parsed
    assert parsed_workspace == workspace_grants
    assert parsed_services == service_grants


def test_parse_grants_toml_reports_malformation_as_none() -> None:
    assert _parse_grants_toml("not toml [[") is None


def test_parse_grants_toml_tolerates_wrong_shapes_as_empty() -> None:
    # The value's meaning is unambiguous, just wrong.
    parsed = _parse_grants_toml("workspace = 'not-a-table'")
    assert parsed is not None
    workspace_grants, service_grants = parsed
    assert workspace_grants == {"users": [], "emails": [], "email_domains": []}
    assert service_grants == {}


def test_parse_grants_toml_reads_a_document_written_before_users_existed() -> None:
    # A grants file from before this change has no ``users`` key; it reads as
    # an empty list rather than as malformed.
    parsed = _parse_grants_toml('[workspace]\nemails = ["a@example.com"]\nemail_domains = []\n')
    assert parsed is not None
    workspace_grants, _service_grants = parsed
    assert workspace_grants == {"users": [], "emails": ["a@example.com"], "email_domains": []}


def test_describe_connector_failure_reports_an_expired_session() -> None:
    # Not "signed out": the account is still in this device's credential list,
    # so the app goes on showing it as signed in.
    exc = ImbueCloudCliError("shares create failed: Refresh rejected by connector: Session missing in db")
    message = describe_connector_failure(exc)
    assert message == "Your Imbue Cloud session has expired. You may need to log out and log in again."
    assert "signed out" not in message


def test_describe_connector_failure_reports_an_unverified_email() -> None:
    exc = ImbueCloudCliError('sync records push failed: Unauthenticated (401): {"detail":"Email not verified"}')
    assert describe_connector_failure(exc) == (
        "Imbue Cloud has not verified this account's email address. Verify it, then retry."
    )


def test_describe_connector_failure_keeps_an_unrecognized_message() -> None:
    # Better the connector's own wording than a pointer to a log file.
    exc = ImbueCloudCliError("shares create failed: Connector error 500: upstream exploded")
    assert describe_connector_failure(exc) == "shares create failed: Connector error 500: upstream exploded"


def test_resolve_agent_for_host_falls_back_to_the_workspace_record(tmp_path: Path) -> None:
    """A stopped (undiscovered) machine still resolves via its active workspace record.

    Without the fallback, the share panel of a stopped machine read as "not
    shared" and disable returned 502 even while a connector share was active.
    """
    agent_id = AgentId.generate()
    host_id = str(HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-rec-1", email="rec@example.com")
    store = make_session_store_for_test(tmp_path, cli=cli)
    store.associate_created_workspace(
        user_id="user-rec-1",
        agent_id=str(agent_id),
        host_id=host_id,
        display_name="stopped-machine",
        color=None,
        is_cloud_row=False,
    )
    undiscovered = StaticBackendResolver(url_by_agent_and_service={})

    assert resolve_agent_for_host(undiscovered, host_id, store) == agent_id


def test_resolve_agent_for_host_raises_when_neither_discovery_nor_records_know_the_host(tmp_path: Path) -> None:
    store = make_session_store_for_test(tmp_path, cli=make_fake_imbue_cloud_cli())
    undiscovered = StaticBackendResolver(url_by_agent_and_service={})

    with pytest.raises(SharingError, match="No workspace is known"):
        resolve_agent_for_host(undiscovered, str(HostId.generate()), store)


def _unpublished_workspace_for_test(tmp_path: Path, caller: MngrCaller) -> tuple[str, StaticBackendResolver, Any]:
    """A machine with no connector share whose workspace answers execs through ``caller``."""
    agent_id = AgentId.generate()
    host_id = str(HostId.generate())
    cli = FakeImbueCloudCli(connector_url=FAKE_CONNECTOR_URL, mngr_caller=caller)
    cli.add_account(user_id="user-unpublished-1", email="owner@example.com")
    store = make_session_store_for_test(tmp_path, cli=cli)
    store.associate_created_workspace(
        user_id="user-unpublished-1",
        agent_id=str(agent_id),
        host_id=host_id,
        display_name="unpublished-machine",
        color=None,
        is_cloud_row=False,
    )
    return host_id, StaticBackendResolver(url_by_agent_and_service={}), (cli, store)


def test_get_sharing_reports_the_grants_of_an_unpublished_workspace(tmp_path: Path) -> None:
    grants_toml = render_grants_toml(
        {"users": [], "emails": ["friend@example.com"], "email_domains": ["partner.org"]}, {}
    )
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=exec_json_envelope(grants_toml)))
    host_id, resolver, (cli, store) = _unpublished_workspace_for_test(tmp_path, caller)

    document = get_sharing(host_id, resolver, cli, store, None, None, None)

    assert document["enabled"] is False
    assert document["url"] is None
    assert document["grants"]["workspace"] == {
        "users": [],
        "emails": ["friend@example.com"],
        "email_domains": ["partner.org"],
    }


def test_get_sharing_reports_an_empty_document_for_a_workspace_that_has_never_been_published(
    tmp_path: Path,
) -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=exec_json_envelope("")))
    host_id, resolver, (cli, store) = _unpublished_workspace_for_test(tmp_path, caller)

    document = get_sharing(host_id, resolver, cli, store, None, None, None)

    assert document["enabled"] is False
    assert document["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}


def test_get_sharing_reports_unknown_grants_when_an_unpublished_workspace_cannot_be_read(tmp_path: Path) -> None:
    # Unreadable must stay distinguishable from empty even while off.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="agent offline"))
    host_id, resolver, (cli, store) = _unpublished_workspace_for_test(tmp_path, caller)

    document = get_sharing(host_id, resolver, cli, store, None, None, None)

    assert document["enabled"] is False
    assert document["grants"] is None


def test_unpublish_workspace_drops_the_forward_identity_entry_and_requests_a_sync(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    host_id = str(HostId.generate())
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-rec-1", email="rec@example.com")
    store = make_session_store_for_test(tmp_path, cli=cli)
    store.associate_created_workspace(
        user_id="user-rec-1",
        agent_id=str(agent_id),
        host_id=host_id,
        display_name="shared-machine",
        color=None,
        is_cloud_row=False,
    )
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    headers_file = ForwardHeadersFile(path=tmp_path / "forward_headers.json")
    sync_requests: list[str] = []
    forward_identity = ForwardIdentityPublisher(
        session_store=store,
        headers_file=headers_file,
        on_shared_workspaces_changed=lambda: sync_requests.append("kick"),
    )
    forward_identity.mark_shared(str(agent_id), "user-rec-1")
    assert str(agent_id) in json.loads(headers_file.path.read_text())
    assert sync_requests == ["kick"]

    unpublish_workspace(host_id, resolver, cli, store, forward_identity)

    assert list(json.loads(headers_file.path.read_text())) == ["*"]
    # A sync is requested only once the connector agrees the share is gone, so
    # the pass it runs cannot re-add the entry from a stale listing.
    assert sync_requests == ["kick", "kick"]


class _UserShowingCli(FakeImbueCloudCli):
    """Fake CLI answering `users show` from a canned map (an absent id is a miss)."""

    identity_by_user_id: dict[str, UserIdentityCliInfo] = Field(default_factory=dict)
    is_show_failing: bool = Field(default=False)
    shown_user_ids: list[str] = Field(default_factory=list, description="Every id `users show` was asked for")

    def show_user(self, *, account: str, user_id: str) -> UserIdentityCliInfo | None:
        self.shown_user_ids.append(user_id)
        if self.is_show_failing:
            raise ImbueCloudCliError("connector down")
        return self.identity_by_user_id.get(user_id)


def test_resolve_grant_identities_serves_fresh_cache_entries_and_fetches_the_rest(tmp_path: Path) -> None:
    cli = _UserShowingCli(
        connector_url=FAKE_CONNECTOR_URL,
        identity_by_user_id={
            "user-fetched": UserIdentityCliInfo(user_id="user-fetched", email="f@example.com", display_name="Fetched")
        },
    )
    cache = IdentityCache(path=tmp_path / "identity_cache.json")
    cache.put(IdentityRecord(user_id="user-cached", email="c@example.com"), now_utc())

    identities = _resolve_grant_identities(
        ["user-cached", "user-fetched", "user-unknown"], cache, cli, "owner@example.com"
    )

    # The unknown id is simply absent: the share panel renders the bare id.
    assert sorted(identities) == ["user-cached", "user-fetched"]
    assert identities["user-fetched"].display_name == "Fetched"
    # A fresh cache entry never costs a lookup, and a fetched record is
    # cached so the next render of the document costs none either.
    assert cli.shown_user_ids == ["user-fetched", "user-unknown"]
    cached = cache.get("user-fetched")
    assert cached is not None and cached.record.email == "f@example.com"


def test_resolve_grant_identities_omits_ids_it_cannot_look_up(tmp_path: Path) -> None:
    cli = _UserShowingCli(connector_url=FAKE_CONNECTOR_URL, is_show_failing=True)
    cache = IdentityCache(path=tmp_path / "identity_cache.json")

    # A failed lookup with nothing cached drops the id rather than failing the document.
    assert _resolve_grant_identities(["user-1"], cache, cli, "owner@example.com") == {}
    assert cli.shown_user_ids == ["user-1"]
    # Without a signed-in account there is nothing to look up with, so no lookup is attempted.
    assert _resolve_grant_identities(["user-1"], cache, cli, None) == {}
    assert cli.shown_user_ids == ["user-1"]
    # An app running without an identity cache resolves nothing.
    assert _resolve_grant_identities(["user-1"], None, cli, "owner@example.com") == {}


def _publish_for_test(
    host_id: str,
    agent_id: AgentId,
    cli: ImbueCloudCli,
    *,
    is_cloud_row: bool,
    grants: SharingGrantsDocument | None = None,
) -> dict[str, Any]:
    """Publish ``host_id`` as its owner, with no service labels known yet."""
    return _publish_workspace_with_cli(
        host_id,
        agent_id,
        str(agent_id),
        cli,
        "owner@example.com",
        _client_env_config(),
        is_cloud_row=is_cloud_row,
        service_labels={},
        identity_cache=None,
        forward_identity=None,
        owner_account=None,
        grants=grants,
    )


_STALE_DOMAIN = "host-" + "d" * 32 + ".owner1234.us1.retired.example"
_MOVED_DOMAIN = "f" * 32 + ".owner1234.us1.shares.example"


def _stale_share(host_id: str) -> ShareCliInfo:
    return ShareCliInfo(
        host_id=host_id, workspace_domain=_STALE_DOMAIN, region="us1", state="active", needs_reshare=True
    )


def _migrate_for_test(host_id: str, agent_id: AgentId, cli: ImbueCloudCli) -> dict[str, Any]:
    return migrate_stale_share(
        host_id,
        agent_id,
        str(agent_id),
        _stale_share(host_id),
        cli,
        "owner@example.com",
        _client_env_config(),
        {"system_interface": "shell-r4nd"},
        None,
        None,
        None,
    )


def test_migrate_stale_share_reshares_and_rewrites_only_share_env() -> None:
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL, created_workspace_domain_to_return=_MOVED_DOMAIN)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    grants_toml = render_grants_toml({"users": ["user-9"], "emails": ["friend@example.com"], "email_domains": []}, {})
    caller.result = make_share_probe_result(
        is_gateway_present=True, is_share_env_present=True, grants_toml_text=grants_toml
    )
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    document = _migrate_for_test(host_id, agent_id, cli)

    # The re-share keys the workspace, names the shell's label, and never
    # steers the region: the connector keeps the share's own.
    assert cli.create_share_calls == [("owner@example.com", host_id, "shell-r4nd", None, str(agent_id))]
    exec_calls = [call for call in caller.calls if call and call[0] == "exec"]
    assert len(exec_calls) == 2
    assert "MNGR_SHARE_GATEWAY" in exec_calls[0][2]
    write_command = exec_calls[1][2]
    assert "data/.secrets/share.env" in write_command
    assert "share_grants.toml" not in write_command
    assert f"SHARE_WORKSPACE_DOMAIN={_MOVED_DOMAIN}" in read_injected_share_env_text(cli)
    assert document["migrated_domain_from"] == _STALE_DOMAIN
    assert document["workspace_domain"] == _MOVED_DOMAIN
    assert document["enabled"] is True
    # The grants the workspace holds ride the document unchanged.
    assert document["grants"]["workspace"] == {
        "users": ["user-9"],
        "emails": ["friend@example.com"],
        "email_domains": [],
    }


def test_migrate_stale_share_moves_a_share_whose_grants_document_cannot_be_parsed() -> None:
    # The move rewrites share.env alone and leaves the document untouched, so
    # one that cannot be parsed costs it nothing.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL, created_workspace_domain_to_return=_MOVED_DOMAIN)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(
        is_gateway_present=True, is_share_env_present=True, grants_toml_text="[workspace\nemails = ["
    )

    document = _migrate_for_test("host-" + "d" * 32, AgentId("agent-" + "c" * 32), cli)

    assert document["workspace_domain"] == _MOVED_DOMAIN
    write_command = [call for call in caller.calls if call and call[0] == "exec"][1][2]
    assert "share_grants.toml" not in write_command


def test_migrate_stale_share_refuses_a_pre_share_gateway_workspace() -> None:
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=False, is_share_env_present=True)

    with pytest.raises(SharingError, match="update itself"):
        _migrate_for_test("host-" + "d" * 32, AgentId("agent-" + "c" * 32), cli)

    assert cli.create_share_calls == []


def test_migrate_stale_share_names_the_recovery_when_the_write_fails_after_the_share_moved() -> None:
    # The connector create moves the share before the materials write, and
    # the panel's retry is a read that will not inject again: the failure has
    # to say that only turning publishing off and on again brings the links back.
    caller = ScriptedMngrCaller(
        results=(
            make_share_probe_result(is_gateway_present=True, is_share_env_present=True),
            MngrCallResult(returncode=1, stderr="exec died"),
        )
    )
    cli = SucceedingCreateShareCli(
        connector_url=FAKE_CONNECTOR_URL, created_workspace_domain_to_return=_MOVED_DOMAIN, mngr_caller=caller
    )

    with pytest.raises(SharingError, match="turned off and on again") as exc_info:
        _migrate_for_test("host-" + "d" * 32, AgentId("agent-" + "c" * 32), cli)

    assert len(cli.create_share_calls) == 1
    assert "exec died" in str(exc_info.value)


def test_publish_workspace_cloud_row_uses_the_client_side_share_create() -> None:
    # An unpublished imbue_cloud row provisions exactly like a local one:
    # connector ``shares create`` plus materials injection over the user's own
    # SSH. (The connector's server-side primitive is web-create-only.)
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    document = _publish_for_test(host_id, agent_id, cli, is_cloud_row=True)

    assert cli.create_share_calls == [("owner@example.com", host_id, None, None, str(agent_id))]
    # Exactly TWO execs touch the workspace: the one-shot state probe and the
    # combined write of the seeded grants document + share.env. Each exec pays
    # a full mngr process + SSH round trip on a remote host, so the count is
    # the contract.
    exec_calls = [call for call in caller.calls if call and call[0] == "exec"]
    assert len(exec_calls) == 2
    probe_command, write_command = exec_calls[0][2], exec_calls[1][2]
    assert "system/services/share_gateway" in probe_command
    assert "share_grants.toml" in write_command
    assert "data/.secrets/share.env" in write_command
    # The owner's identity rides requests, never a file in the workspace.
    assert "owner_email" not in write_command
    assert document["enabled"] is True
    # Publishing grants nobody: the gateway admits the owner on its own, so
    # nothing is seeded on their behalf.
    assert document["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}


def test_publish_workspace_replaces_a_document_nothing_can_parse() -> None:
    # An unparseable document must never wedge a workspace: publishing treats
    # it as no document at all and seeds the empty one the gateway needs.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(
        is_gateway_present=True, is_share_env_present=False, grants_toml_text="[workspace\nemails = ["
    )
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    document = _publish_for_test(host_id, agent_id, cli, is_cloud_row=False)

    assert document["enabled"] is True
    assert document["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}
    write_command = [call for call in caller.calls if call and call[0] == "exec"][1][2]
    assert "share_grants.toml" in write_command


def test_publish_workspace_seeds_a_document_an_already_published_workspace_cannot_parse() -> None:
    # The seed does not ride only the create: a workspace whose share is
    # already active but whose document cannot be read gets one written too,
    # or the gateway would go on refusing everyone with nothing to fix.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(
        is_gateway_present=True, is_share_env_present=True, grants_toml_text="[workspace\nemails = ["
    )
    host_id = "host-" + "d" * 32
    cli.add_share("owner@example.com", host_id)

    document = _publish_for_test(host_id, AgentId("agent-" + "c" * 32), cli, is_cloud_row=False)

    assert document["grants"] == {"workspace": {"users": [], "emails": [], "email_domains": []}, "services": {}}
    assert cli.create_share_calls == []
    write_commands = [call[2] for call in caller.calls if call and call[0] == "exec"][1:]
    assert len(write_commands) == 1
    assert "share_grants.toml" in write_commands[0]
    assert "data/.secrets/share.env" not in write_commands[0]


def test_publish_workspace_stores_a_document_it_is_given_in_the_same_write_as_share_env() -> None:
    # The compat route publishes and saves in one operation: the document it
    # carries is checked, normalized, and lands with share.env in one exec.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    given = SharingGrantsDocument(workspace=SharingGrantList(emails=(" Friend@Example.com ",)))

    document = _publish_for_test(
        "host-" + "d" * 32, AgentId("agent-" + "c" * 32), cli, is_cloud_row=False, grants=given
    )

    assert document["grants"]["workspace"]["emails"] == ["friend@example.com"]
    write_commands = [call[2] for call in caller.calls if call and call[0] == "exec"][1:]
    assert len(write_commands) == 1
    assert "share_grants.toml" in write_commands[0]
    assert "data/.secrets/share.env" in write_commands[0]


def test_publish_workspace_refuses_a_document_it_is_given_before_creating_a_share() -> None:
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    refusable = SharingGrantsDocument(workspace=SharingGrantList(email_domains=("gmail.com",)))

    with pytest.raises(GrantsRefusedError):
        _publish_for_test("host-" + "d" * 32, AgentId("agent-" + "c" * 32), cli, is_cloud_row=False, grants=refusable)

    assert cli.create_share_calls == []
    assert len([call for call in caller.calls if call and call[0] == "exec"]) == 1


def test_publish_workspace_stamps_the_connector_reported_chrome_origin_into_share_env() -> None:
    # On tiers whose web chrome lives on a custom domain (deploy.toml
    # [origins].chrome_origin), the connector reports that origin on the share
    # create; stamping anything else (e.g. the bare connector URL) locks the
    # real /web chrome out of the workspace's frame-ancestors CSP.
    cli = SucceedingCreateShareCli(
        connector_url=FAKE_CONNECTOR_URL, chrome_origin_to_return="https://minds.shares.example"
    )
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    _publish_for_test(host_id, agent_id, cli, is_cloud_row=False)

    share_env_text = read_injected_share_env_text(cli)
    assert "export SHARE_CHROME_ORIGIN=https://minds.shares.example\n" in share_env_text
    connector_url = str(FAKE_CONNECTOR_URL).rstrip("/")
    assert f"SHARE_CHROME_ORIGIN={connector_url}" not in share_env_text


def test_publish_workspace_falls_back_to_the_connector_origin_without_a_reported_chrome_origin() -> None:
    # An old connector (or a tier with no hosted chrome configured) reports no
    # chrome origin; the pre-field behavior -- the bare connector origin, where
    # dev tiers path-serve the chrome -- must be preserved exactly.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    _publish_for_test(host_id, agent_id, cli, is_cloud_row=False)

    connector_url = str(FAKE_CONNECTOR_URL).rstrip("/")
    assert f"export SHARE_CHROME_ORIGIN={connector_url}\n" in read_injected_share_env_text(cli)


@pytest.mark.parametrize("is_cloud_row", [True, False])
def test_publish_workspace_refuses_a_pre_share_gateway_workspace(is_cloud_row: bool) -> None:
    # A workspace created from a template older than the share gateway has
    # nothing watching share.env: the publish is refused up front with the
    # update-self pointer instead of provisioning a share that can never come
    # up. The probe is the first exec, so a failing exec refuses immediately.
    cli = make_fake_imbue_cloud_cli()
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = MngrCallResult(returncode=1, stderr="test -d failed")
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    with pytest.raises(SharingError, match="update itself"):
        _publish_for_test(host_id, agent_id, cli, is_cloud_row=is_cloud_row)

    # Nothing was provisioned: no connector share, no injection past the probe.
    assert cli.shares_by_account == {}
    exec_calls = [call for call in caller.calls if call and call[0] == "exec"]
    assert len(exec_calls) == 1
    assert "system/services/share_gateway" in exec_calls[0][2]
    assert exec_calls[0][-3:] == ["--no-start", "--format", "json"]


class _PreferredRegionRecordingCli(FakeImbueCloudCli):
    """Records ``create_share``'s ``preferred_region``, then fails so the flow stops at the create.

    Same seam-pinning pattern as ``_RecordingCreateShareCli`` in
    ``workspace_create_web_access_test.py``: the raise keeps the test at the
    call under test instead of continuing into materials injection.
    """

    recorded_preferred_regions: list[str | None] = Field(
        default_factory=list, description="preferred_region for every create_share call, in order"
    )
    relay_list_call_count: int = Field(default=0, description="How many times list_share_relays was consulted")

    def list_share_relays(self, *, account: str) -> dict[str, tuple[str, ...]]:
        self.relay_list_call_count += 1
        return super().list_share_relays(account=account)

    def create_share(
        self,
        *,
        account: str,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareCliInfo:
        self.recorded_preferred_regions.append(preferred_region)
        raise ImbueCloudCliError("recorded; stopping the bring-up here")


def test_publish_workspace_first_time_local_share_passes_the_measured_preferred_region() -> None:
    # A first-time local share (no existing share record) steers the relay by
    # measured latency. A single configured region short-circuits the
    # measurement (no sockets are opened), but the picked region must still be
    # forwarded to the connector's create.
    cli = _PreferredRegionRecordingCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    cli.relays_to_return = {"us9": ("relay-us9.example:7000",)}
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    with pytest.raises(SharingError):
        _publish_for_test(host_id, agent_id, cli, is_cloud_row=False)

    assert cli.recorded_preferred_regions == ["us9"]
    assert cli.relay_list_call_count == 1


def test_publish_workspace_re_share_still_measures_but_the_preference_is_advisory() -> None:
    # A local publish with no materials in the workspace measures relay latency
    # and passes the result as preferred_region. The connector honors the
    # preference only for hosts it has no region record of, so an existing share
    # keeps its region -- and the common publish path never has to consult the
    # connector's status first to tell the two apart.
    cli = _PreferredRegionRecordingCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    cli.relays_to_return = {"us9": ("relay-us9.example:7000",)}
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32
    cli.shares_by_account.setdefault("owner@example.com", {})[host_id] = "inactive"

    with pytest.raises(SharingError):
        _publish_for_test(host_id, agent_id, cli, is_cloud_row=False)

    assert cli.recorded_preferred_regions == ["us9"]
    assert cli.relay_list_call_count == 1


def test_publish_workspace_with_stale_materials_reprovisions_without_measuring() -> None:
    # Materials present but the connector says the share is inactive (disabled
    # from another device): the flow consults the status (the one path that
    # still needs it), then falls through to a full re-provisioning create --
    # with no latency measurement, since the workspace side is already placed.
    cli = _PreferredRegionRecordingCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=True)
    cli.relays_to_return = {"us9": ("relay-us9.example:7000",)}
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32
    cli.shares_by_account.setdefault("owner@example.com", {})[host_id] = "inactive"

    with pytest.raises(SharingError):
        _publish_for_test(host_id, agent_id, cli, is_cloud_row=False)

    assert cli.recorded_preferred_regions == [None]
    assert cli.relay_list_call_count == 0


def test_publish_workspace_cloud_row_skips_the_relay_latency_measurement() -> None:
    # A cloud row's workspace runs on a pool host, so the desktop's own relay
    # latency says nothing about it: no relays are probed and no preference is
    # sent (the connector applies its default region). The raise from the
    # recording create also proves a connector refusal surfaces as SharingError.
    cli = _PreferredRegionRecordingCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    cli.relays_to_return = {"us1": ("relay-us1.example:7000",), "us2": ("relay-us2.example:7000",)}
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32

    with pytest.raises(SharingError):
        _publish_for_test(host_id, agent_id, cli, is_cloud_row=True)

    assert cli.recorded_preferred_regions == [None]
    assert cli.relay_list_call_count == 0


class _StatusOrderRecordingCli(SucceedingCreateShareCli):
    """Records how many execs had run when the connector's share status was read."""

    exec_counts_at_status_read: list[int] = Field(default_factory=list)

    def get_share_status(self, *, account: str, host_id: str) -> ShareCliInfo | None:
        caller = self.mngr_caller
        assert isinstance(caller, RecordingMngrCaller)
        self.exec_counts_at_status_read.append(len([call for call in caller.calls if call and call[0] == "exec"]))
        return super().get_share_status(account=account, host_id=host_id)


def _save_grants_for_test(
    host_id: str, agent_id: AgentId, grants: SharingGrantsDocument, cli: ImbueCloudCli
) -> dict[str, Any]:
    """Save ``grants`` onto ``host_id`` as its owner, with no service labels known yet."""
    return _save_grants_with_cli(
        host_id, str(agent_id), grants, cli, "owner@example.com", service_labels={}, identity_cache=None
    )


def test_save_grants_refuses_a_pre_share_gateway_workspace() -> None:
    # Nothing in such a workspace reads the document, so writing one would only
    # look like it worked.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=False, is_share_env_present=False)

    with pytest.raises(SharingError, match="update itself"):
        _save_grants_for_test("host-" + "d" * 32, AgentId("agent-" + "c" * 32), SharingGrantsDocument(), cli)

    assert len([call for call in caller.calls if call and call[0] == "exec"]) == 1


def test_save_grants_overwrites_a_document_nothing_can_parse() -> None:
    # An unparseable document grandfathers nothing and is replaced, so a
    # corrupted file cannot lock the list.
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(
        is_gateway_present=True, is_share_env_present=False, grants_toml_text="[workspace\nemails = ["
    )
    grants = SharingGrantsDocument(workspace=SharingGrantList(emails=("friend@example.com",)))

    document = _save_grants_for_test("host-" + "d" * 32, AgentId("agent-" + "c" * 32), grants, cli)

    assert document["grants"]["workspace"]["emails"] == ["friend@example.com"]
    write_command = [call for call in caller.calls if call and call[0] == "exec"][1][2]
    assert "share_grants.toml" in write_command


def test_save_grants_reads_the_connector_before_it_writes() -> None:
    # A connector hiccup must not be reported after the document landed.
    host_id = "host-" + "d" * 32
    cli = _StatusOrderRecordingCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=True)
    cli.add_share("owner@example.com", host_id)
    grants = SharingGrantsDocument(workspace=SharingGrantList(emails=("friend@example.com",)))

    document = _save_grants_for_test(host_id, AgentId("agent-" + "c" * 32), grants, cli)

    assert document["enabled"] is True
    # Read once, after the probe and before the write exec.
    assert cli.exec_counts_at_status_read == [1]
    assert len([call for call in caller.calls if call and call[0] == "exec"]) == 2
