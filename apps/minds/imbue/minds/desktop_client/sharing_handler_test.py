from pathlib import Path

import httpx
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
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import _enable_sharing_with_cli
from imbue.minds.desktop_client.sharing_handler import _parse_grants_toml
from imbue.minds.desktop_client.sharing_handler import describe_connector_failure
from imbue.minds.desktop_client.sharing_handler import pick_lowest_latency_relay_region
from imbue.minds.desktop_client.sharing_handler import probe_share_readiness
from imbue.minds.desktop_client.sharing_handler import resolve_agent_for_host
from imbue.minds.desktop_client.sharing_handler import split_relay_endpoint
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
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
    workspace_grants = {"emails": ["bob@example.com"], "email_domains": ["partner.org"]}
    service_grants = {
        "web": {"emails": ["carol@example.com"], "email_domains": []},
        "my-app": {"emails": [], "email_domains": ["viewer.dev"]},
    }

    rendered = render_grants_toml(workspace_grants, service_grants)
    parsed = _parse_grants_toml(rendered)

    assert parsed is not None
    parsed_workspace, parsed_services = parsed
    assert parsed_workspace == workspace_grants
    assert parsed_services == service_grants


def test_parse_grants_toml_reports_malformation_as_none() -> None:
    # Malformed must stay distinguishable from empty: rendered as "no grants",
    # the next whole-document save would erase every real grant.
    assert _parse_grants_toml("not toml [[") is None


def test_parse_grants_toml_tolerates_wrong_shapes_as_empty() -> None:
    # Valid TOML of the wrong shape parses to an empty scope (there is no
    # hidden grant to protect: the value's meaning is unambiguous, just wrong).
    parsed = _parse_grants_toml("workspace = 'not-a-table'")
    assert parsed is not None
    workspace_grants, service_grants = parsed
    assert workspace_grants == {"emails": [], "email_domains": []}
    assert service_grants == {}


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

    Without the fallback, the Share pane of a stopped machine read as "not
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


def test_enable_sharing_cloud_row_uses_the_client_side_share_create() -> None:
    # An unshared imbue_cloud row provisions exactly like a local one: connector
    # ``shares create`` plus materials injection over the user's own SSH. (The
    # connector's server-side enable-sharing primitive is web-create-only.)
    cli = SucceedingCreateShareCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32
    grants = {"emails": ["owner@example.com", "friend@example.com"], "email_domains": []}

    document = _enable_sharing_with_cli(
        host_id, agent_id, grants, {}, cli, "owner@example.com", _client_env_config(), is_cloud_row=True
    )

    assert cli.create_share_calls == [("owner@example.com", host_id, None, None)]
    # Exactly TWO execs touch the workspace: the one-shot state probe and the
    # combined write of grants + owner email + share.env. Each exec pays a full
    # mngr process + SSH round trip on a remote host, so the count is the
    # contract, not an implementation detail.
    exec_calls = [call for call in caller.calls if call and call[0] == "exec"]
    assert len(exec_calls) == 2
    probe_command, write_command = exec_calls[0][2], exec_calls[1][2]
    assert "system/services/share_gateway" in probe_command
    assert "share_grants.toml" in write_command
    assert "data/.secrets/share.env" in write_command
    assert "data/.state/share/owner_email" in write_command
    assert document["enabled"] is True
    assert document["grants"]["workspace"] == grants


@pytest.mark.parametrize("is_cloud_row", [True, False])
def test_enable_sharing_refuses_a_pre_share_gateway_workspace(is_cloud_row: bool) -> None:
    # A workspace created from a template older than the share gateway has
    # nothing watching share.env: the enable is refused up front with the
    # update-self pointer instead of provisioning a share that can never come
    # up. The probe is the first exec, so a failing exec refuses immediately.
    cli = make_fake_imbue_cloud_cli()
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = MngrCallResult(returncode=1, stderr="test -d failed")
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32
    grants = {"emails": ["owner@example.com"], "email_domains": []}

    with pytest.raises(SharingError, match="update itself"):
        _enable_sharing_with_cli(
            host_id, agent_id, grants, {}, cli, "owner@example.com", _client_env_config(), is_cloud_row=is_cloud_row
        )

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
    ) -> ShareCliInfo:
        self.recorded_preferred_regions.append(preferred_region)
        raise ImbueCloudCliError("recorded; stopping the bring-up here")


def test_enable_sharing_first_time_local_share_passes_the_measured_preferred_region() -> None:
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
    grants = {"emails": ["owner@example.com"], "email_domains": []}

    with pytest.raises(SharingError):
        _enable_sharing_with_cli(
            host_id, agent_id, grants, {}, cli, "owner@example.com", _client_env_config(), is_cloud_row=False
        )

    assert cli.recorded_preferred_regions == ["us9"]
    assert cli.relay_list_call_count == 1


def test_enable_sharing_re_share_still_measures_but_the_preference_is_advisory() -> None:
    # A local enable with no materials in the workspace (first share or
    # re-share after a disable) measures relay latency and passes the result as
    # preferred_region. For a re-share this is deliberate and harmless: the
    # connector honors the preference only for hosts it has no region record
    # of, so an existing share keeps its region -- and the common enable path
    # never has to consult the connector's status first to tell the two apart.
    cli = _PreferredRegionRecordingCli(connector_url=FAKE_CONNECTOR_URL)
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = make_share_probe_result(is_gateway_present=True, is_share_env_present=False)
    cli.relays_to_return = {"us9": ("relay-us9.example:7000",)}
    agent_id = AgentId("agent-" + "c" * 32)
    host_id = "host-" + "d" * 32
    cli.shares_by_account.setdefault("owner@example.com", {})[host_id] = "inactive"
    grants = {"emails": ["owner@example.com"], "email_domains": []}

    with pytest.raises(SharingError):
        _enable_sharing_with_cli(
            host_id, agent_id, grants, {}, cli, "owner@example.com", _client_env_config(), is_cloud_row=False
        )

    assert cli.recorded_preferred_regions == ["us9"]
    assert cli.relay_list_call_count == 1


def test_enable_sharing_with_stale_materials_reprovisions_without_measuring() -> None:
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
    grants = {"emails": ["owner@example.com"], "email_domains": []}

    with pytest.raises(SharingError):
        _enable_sharing_with_cli(
            host_id, agent_id, grants, {}, cli, "owner@example.com", _client_env_config(), is_cloud_row=False
        )

    assert cli.recorded_preferred_regions == [None]
    assert cli.relay_list_call_count == 0


def test_enable_sharing_cloud_row_skips_the_relay_latency_measurement() -> None:
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
    grants = {"emails": ["owner@example.com"], "email_domains": []}

    with pytest.raises(SharingError):
        _enable_sharing_with_cli(
            host_id, agent_id, grants, {}, cli, "owner@example.com", _client_env_config(), is_cloud_row=True
        )

    assert cli.recorded_preferred_regions == [None]
    assert cli.relay_list_call_count == 0
