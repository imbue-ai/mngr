from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.interfaces.data_types import ProviderResourceInfo
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.backend import AGENT_TAG_PREFIX
from imbue.mngr_azure.backend import AzureProvider
from imbue.mngr_azure.backend import AzureProviderBackend
from imbue.mngr_azure.backend import ParsedAzureBuildOptions
from imbue.mngr_azure.backend import _build_idle_watcher_service_unit
from imbue.mngr_azure.backend import _build_self_deallocate_script
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_vps.testing import seed_stopped_host_record


class _SubnetStubClient(AzureVpsClient):
    """AzureVpsClient with subnet resolution stubbed, for hermetic create-hook tests.

    The real ``resolve_subnet_id`` makes an Azure network API call. The pre-create
    hook now invokes it, so tests that exercise the hook stub it: it returns a
    placeholder id, or (when ``stub_subnet_missing``) raises the same
    ``mngr azure prepare`` MngrError the real method raises on a 404.
    """

    stub_subnet_missing: bool = False

    def resolve_subnet_id(self) -> str:
        if self.stub_subnet_missing:
            raise MngrError(
                f"Azure subnet {self.subnet_name!r} (vnet {self.vnet_name!r}, resource group "
                f"{self.resource_group!r}) does not exist in region {self.region!r}. "
                "Run `mngr azure prepare` once to create it, then retry the create."
            )
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}/subnets/{self.subnet_name}"


def _build_provider(
    mngr_ctx: MngrContext, *, auto_shutdown_seconds: int | None, subnet_missing: bool = False
) -> AzureProvider:
    """Construct an AzureProvider with the given auto-shutdown and subnet settings.

    Uses a placeholder credential and a subnet-stubbed client: the create-hook and
    build-args tests that use this helper never make a real Azure API call.
    """
    config = AzureProviderConfig(subscription_id="sub-123", auto_shutdown_seconds=auto_shutdown_seconds)
    client = _SubnetStubClient(
        credential=object(),
        subscription_id="sub-123",
        region=config.default_region,
        resource_group=config.resource_group,
        vnet_name=config.vnet_name,
        subnet_name=config.subnet_name,
        nsg_name=config.nsg_name,
        stub_subnet_missing=subnet_missing,
    )
    return AzureProvider(
        name=ProviderInstanceName("azure-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        azure_client=client,
        azure_config=config,
    )


def test_backend_name_and_config_class() -> None:
    assert str(AzureProviderBackend.get_name()) == "azure"
    assert AzureProviderBackend.get_config_class() is AzureProviderConfig


def test_backend_build_args_help_mentions_azure_specific_args() -> None:
    help_text = AzureProviderBackend.get_build_args_help()
    assert "--azure-region=" in help_text
    assert "--azure-vm-size=" in help_text
    assert "--azure-spot" in help_text


def test_build_provider_instance_raises_provider_unavailable_without_subscription(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An unresolvable subscription means Azure was never reached, so its state is
    # unknown: the backend must raise ProviderUnavailableError (warned by read
    # paths), NOT ProviderEmptyError (silently skipped) -- otherwise a transient
    # read failure would silently drop azure agents from `mngr list`.
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    # Isolate AZURE_CONFIG_DIR (the conftest autouse fixture pins it at the real
    # ~/.azure) so the az-default-subscription fallback resolves nothing here.
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    config = AzureProviderConfig()
    with pytest.raises(ProviderUnavailableError):
        AzureProviderBackend.build_provider_instance(
            name=ProviderInstanceName("azure"), config=config, mngr_ctx=temp_mngr_ctx
        )


def test_unavailable_error_help_text_is_azure_curated_not_start_docker(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unresolvable-provider error must give cloud-auth guidance, not 'start Docker'.

    The generic ProviderUnavailableError help text tells the user to start Docker,
    which is wrong advice for an Azure subscription/credential failure.
    """
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    config = AzureProviderConfig()
    with pytest.raises(ProviderUnavailableError) as exc_info:
        AzureProviderBackend.build_provider_instance(
            name=ProviderInstanceName("azure"), config=config, mngr_ctx=temp_mngr_ctx
        )
    help_text = exc_info.value.user_help_text
    assert help_text is not None
    assert "Docker" not in help_text
    assert "AZURE_SUBSCRIPTION_ID" in help_text
    assert "az login" in help_text
    assert "mngr azure prepare" in help_text


def test_validate_provider_args_under_pytest_raises_when_unset(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=None)
    with pytest.raises(MngrError, match="auto_shutdown_seconds"):
        provider._validate_provider_args_for_create()


def test_validate_provider_args_under_pytest_accepts_positive(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    # Should not raise: auto_shutdown is set and the subnet pre-flight resolves
    # (the stub client reports the prepared subnet present).
    provider._validate_provider_args_for_create()


def test_validate_provider_args_raises_when_subnet_missing(temp_mngr_ctx: MngrContext) -> None:
    """The read-only subnet pre-flight fires before any VM write when prepare wasn't run.

    A first-time user who skipped ``mngr azure prepare`` should get the clean
    prepare-pointer error from the pre-create hook, not mid-create under a
    "Host creation failed, attempting cleanup..." line.
    """
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600, subnet_missing=True)
    with pytest.raises(MngrError, match="mngr azure prepare"):
        provider._validate_provider_args_for_create()


def test_parse_build_args_uses_defaults_when_none(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    parsed = provider._parse_build_args(None)
    assert isinstance(parsed, ParsedAzureBuildOptions)
    assert parsed.region == "westus"
    assert parsed.plan == "Standard_B2s"
    assert parsed.spot is False
    assert parsed.git_depth is None


def test_parse_build_args_extracts_azure_knobs_plus_docker_passthrough(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    parsed = provider._parse_build_args(
        ["--azure-region=eastus", "--azure-vm-size=Standard_D2s_v5", "--azure-spot", "--file=Dockerfile", "."]
    )
    assert parsed.region == "eastus"
    assert parsed.plan == "Standard_D2s_v5"
    assert parsed.spot is True
    assert parsed.docker_build_args == ("--file=Dockerfile", ".")


def test_parse_build_args_rejects_unknown_azure_flag(temp_mngr_ctx: MngrContext) -> None:
    provider = _build_provider(temp_mngr_ctx, auto_shutdown_seconds=3600)
    with pytest.raises(MngrError, match="Unknown azure build arg"):
        provider._parse_build_args(["--azure-bogus=1"])


# =============================================================================
# Offline tag paths (stop/start lifecycle): instance lookup, agent-data mirror,
# discovery, offline-host reconstruction.
#
# These build an AzureProvider over a _StubbedAzureVpsClient whose fake compute
# client returns hand-built VM SimpleNamespaces; the provider normalizes them
# through the real list_instances path (tags -> "key=value" list, power state ->
# "state"). The Azure analog of the AWS/GCP backend offline tests.
# =============================================================================


def _build_stubbed_provider(
    mngr_ctx: MngrContext,
) -> tuple[AzureProvider, FakeComputeClient, FakeResourceClient]:
    """Build an AzureProvider whose Azure client is a _StubbedAzureVpsClient over fakes.

    Returns the provider, the fake compute client (so a test can seed
    ``virtual_machines.list_result`` -- the tag-based lookups read it) and the fake
    resource client (so a test can assert on the recorded tag-update patches).
    Mirrors the AWS / GCP ``_build_stubbed_provider``.
    """
    config = AzureProviderConfig(subscription_id="sub-123", auto_shutdown_seconds=3600)
    compute = FakeComputeClient()
    resource = FakeResourceClient()
    client = _StubbedAzureVpsClient(
        credential=object(),
        subscription_id="sub-123",
        region=config.default_region,
        resource_group=config.resource_group,
        stubbed_compute_client=compute,
        stubbed_network_client=FakeNetworkClient(),
        stubbed_resource_client=resource,
        stubbed_authorization_client=FakeAuthorizationClient(),
    )
    provider = AzureProvider(
        name=ProviderInstanceName("azure-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        azure_client=client,
        azure_config=config,
    )
    return provider, compute, resource


def _vm(name: str, *, tags: dict[str, str] | None = None) -> SimpleNamespace:
    """A VM SimpleNamespace as the fake compute client's ``list`` returns it.

    The resource-group list carries no power state (``expand=instanceView`` is
    rejected on it), so the normalized dict's ``state`` is always empty. Tags
    become the normalized ``"key=value"`` list. The provider's instance lookups go
    through ``list_instances(provider_tag="azure-test")``, which filters on the
    ``mngr-provider`` tag, so it is injected by default (every mngr VM carries it)
    unless a test sets its own.
    """
    full_tags = {"mngr-provider": "azure-test", **(tags or {})}
    return SimpleNamespace(name=name, tags=full_tags, instance_view=None)


def _seed_compute(compute: FakeComputeClient, vms: list[SimpleNamespace]) -> None:
    """Seed the fake compute VM list for the normalize path."""
    compute.virtual_machines.list_result = vms


def _set_power_state(compute: FakeComputeClient, power_suffix: str) -> None:
    """Set the shared instance-view result so ``get_instance_status`` maps to one power state.

    ``get_instance_status`` reads the fake's single ``instance_view_result`` (the
    same for every VM); the real client maps the ``PowerState/<suffix>`` status to a
    ``VpsInstanceStatus`` (e.g. ``deallocated``/``stopping`` -> HALTED, ``running``
    -> ACTIVE).
    """
    compute.virtual_machines.instance_view_result = SimpleNamespace(
        statuses=[
            SimpleNamespace(code="ProvisioningState/succeeded"),
            SimpleNamespace(code=f"PowerState/{power_suffix}"),
        ]
    )


def test_find_instance_for_host_matches_by_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """``_find_instance_for_host`` resolves a (deallocated) VM by its mngr-host-id tag, no SSH."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    _seed_compute(
        compute,
        [
            _vm("vm-match", tags={"mngr-host-id": str(host_id), "mngr-provider": "azure-test"}),
            _vm("vm-other", tags={"mngr-host-id": str(HostId.generate()), "mngr-provider": "azure-test"}),
        ],
    )
    found = provider._find_instance_for_host(host_id)
    assert found is not None
    assert found["id"] == "vm-match"


def test_find_instance_for_host_returns_none_when_no_tag_match(temp_mngr_ctx: MngrContext) -> None:
    """A host with no matching VM tag resolves to None (after a single cache-refresh retry)."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    _seed_compute(compute, [_vm("vm-other", tags={"mngr-host-id": str(HostId.generate())})])
    assert provider._find_instance_for_host(HostId.generate()) is None


def test_find_instance_for_host_refuses_duplicate_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """Two VMs sharing a mngr-host-id tag are refused, not silently disambiguated.

    The tag is account-writable, so a duplicate could otherwise steer ``mngr start``
    (and the agent-tag writes keyed off this lookup) onto the wrong VM. The lookup
    must raise rather than pick the first match.
    """
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    _seed_compute(
        compute,
        [
            _vm("vm-real", tags={"mngr-host-id": str(host_id)}),
            _vm("vm-evil", tags={"mngr-host-id": str(host_id)}),
        ],
    )
    with pytest.raises(MngrError, match="ambiguous"):
        provider._find_instance_for_host(host_id)


def test_persist_agent_data_mirrors_fields_into_vm_tags(temp_mngr_ctx: MngrContext) -> None:
    """persist_agent_data finds the VM by host tag and upserts per-field agent tags.

    Exercises the deallocated-host path (the on-volume base write is unavailable, so
    only the VM tags are written); the seeded ``vps_ip=None`` record makes
    ``super().persist_agent_data`` short-circuit with ``HostNotFoundError``. A Merge
    tag patch carries the name/type, plus labels as compact JSON.
    """
    provider, compute, resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    _seed_compute(compute, [_vm("vm-1", tags={"mngr-host-id": str(host_id)})])
    provider.persist_agent_data(
        host_id,
        {"id": str(agent_id), "name": "a1", "type": "command", "labels": {"env": "prod"}},
    )
    assert len(resource.tags.updates) == 1
    _scope, parameters = resource.tags.updates[0]
    assert parameters.operation == "Merge"
    written = parameters.properties.tags
    assert written[f"{AGENT_TAG_PREFIX}{agent_id}-name"] == "a1"
    assert written[f"{AGENT_TAG_PREFIX}{agent_id}-type"] == "command"
    assert written[f"{AGENT_TAG_PREFIX}{agent_id}-labels"] == '{"env":"prod"}'


def test_list_persisted_agent_data_for_host_reads_tags(temp_mngr_ctx: MngrContext) -> None:
    """list_persisted_agent_data_for_host reassembles an agent from its tags (deallocated host)."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={
                    "mngr-host-id": str(host_id),
                    f"{AGENT_TAG_PREFIX}{agent_id}-name": "a1",
                    f"{AGENT_TAG_PREFIX}{agent_id}-type": "command",
                    f"{AGENT_TAG_PREFIX}{agent_id}-labels": '{"env":"prod"}',
                },
            )
        ],
    )
    agents = provider.list_persisted_agent_data_for_host(host_id)
    assert len(agents) == 1
    assert agents[0]["id"] == str(agent_id)
    assert agents[0]["name"] == "a1"
    assert agents[0]["type"] == "command"
    assert agents[0]["labels"] == {"env": "prod"}


def test_discover_hosts_and_agents_surfaces_deallocated_host_from_tags(temp_mngr_ctx: MngrContext) -> None:
    """A deallocated VM is reconstructed from tags as a STOPPED host with its agents."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={
                    "mngr-host-id": str(host_id),
                    "mngr-provider": "azure-test",
                    "mngr-host-name": "mngr-myhost",
                    f"{AGENT_TAG_PREFIX}{agent_id}-name": "a1",
                    f"{AGENT_TAG_PREFIX}{agent_id}-type": "command",
                },
            )
        ],
    )
    _set_power_state(compute, "deallocated")
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    hosts = list(result.keys())
    assert len(hosts) == 1
    assert hosts[0].host_id == host_id
    assert str(hosts[0].host_name) == "myhost"
    assert hosts[0].host_state == HostState.STOPPED
    agents = result[hosts[0]]
    assert len(agents) == 1
    assert agents[0].agent_id == agent_id
    assert str(agents[0].agent_name) == "a1"


def test_discover_hosts_and_agents_surfaces_stopping_host_during_transition(temp_mngr_ctx: MngrContext) -> None:
    """A still-stopping VM (OS down, status HALTED) is reconstructed so it doesn't vanish mid-stop."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={
                    "mngr-host-id": str(host_id),
                    "mngr-provider": "azure-test",
                    "mngr-host-name": "mngr-myhost",
                    f"{AGENT_TAG_PREFIX}{agent_id}-name": "a1",
                },
            )
        ],
    )
    _set_power_state(compute, "stopping")
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    hosts = {host.host_id: host for host in result}
    assert host_id in hosts
    assert hosts[host_id].host_state == HostState.STOPPED
    assert [a.agent_id for a in result[hosts[host_id]]] == [agent_id]


def test_discover_hosts_and_agents_skips_vm_with_absent_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """A VM with no mngr-host-id tag is skipped; a well-formed deallocated host still surfaces."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    good_host_id = HostId.generate()
    _seed_compute(
        compute,
        [
            _vm("vm-bad", tags={"mngr-provider": "azure-test"}),
            _vm(
                "vm-good",
                tags={"mngr-host-id": str(good_host_id), "mngr-provider": "azure-test", "mngr-host-name": "mngr-good"},
            ),
        ],
    )
    _set_power_state(compute, "deallocated")
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    host_ids = {host.host_id for host in result}
    assert good_host_id in host_ids
    assert len(host_ids) == 1


def test_discover_hosts_and_agents_skips_running_but_unreachable_vm(temp_mngr_ctx: MngrContext) -> None:
    """A not-online mngr VM that is still ``running`` is NOT reconstructed as STOPPED.

    The SSH base sweep didn't surface it (no reachable host record), but its
    per-candidate ``get_instance_status`` reports ACTIVE (not HALTED), so the
    transiently-unreachable VM is left out rather than misreported as stopped.
    """
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={
                    "mngr-host-id": str(host_id),
                    "mngr-provider": "azure-test",
                    "mngr-host-name": "mngr-myhost",
                },
            )
        ],
    )
    _set_power_state(compute, "running")
    with ConcurrencyGroup(name="test") as cg:
        result = provider.discover_hosts_and_agents(cg)
    assert host_id not in {host.host_id for host in result}


def test_to_offline_host_reconstructs_stopped_host_from_tags(temp_mngr_ctx: MngrContext) -> None:
    """to_offline_host rebuilds a STOPPED offline host from tags when SSH can't reach it.

    The name comes from the ``mngr-host-name`` tag (``mngr-`` prefix stripped) and
    created_at from the ISO ``mngr-created-at`` tag.
    """
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={
                    "mngr-host-id": str(host_id),
                    "mngr-host-name": "mngr-myhost",
                    "mngr-created-at": "2026-01-01T00:00:00+00:00",
                },
            )
        ],
    )
    offline = provider.to_offline_host(host_id)
    assert offline.id == host_id
    assert str(offline.get_certified_data().host_name) == "myhost"
    assert offline.get_state() == HostState.STOPPED
    created_at = offline.get_certified_data().created_at
    assert (created_at.year, created_at.month, created_at.day) == (2026, 1, 1)


def test_to_offline_host_falls_back_to_now_on_malformed_created_at(
    temp_mngr_ctx: MngrContext, log_warnings: list[str]
) -> None:
    """A malformed mngr-created-at tag is surfaced (warning) and falls back to now(), not swallowed."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    before = datetime.now(timezone.utc)
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={"mngr-host-id": str(host_id), "mngr-host-name": "mngr-myhost", "mngr-created-at": "not-a-stamp"},
            )
        ],
    )
    offline = provider.to_offline_host(host_id)
    assert offline.id == host_id
    assert offline.get_state() == HostState.STOPPED
    assert offline.get_certified_data().created_at >= before
    assert any("Malformed mngr-created-at" in w for w in log_warnings), log_warnings


# =============================================================================
# Agent-tag helpers (the per-field upsert/delete logic, unit-level)
# =============================================================================


def _normalized_instance(tag_pairs: dict[str, str]) -> dict:
    """A normalized instance dict (``{"id", "tags": ["k=v", ...]}``) for tag-helper unit tests."""
    return {"id": "vm-1", "tags": [f"{k}={v}" for k, v in tag_pairs.items()]}


def test_agent_field_items_builds_one_tag_per_field(temp_mngr_ctx: MngrContext) -> None:
    """name/type/labels each map to their own mngr-agent-<id>-<field> tag; the id is in the key."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    set_tags, delete_keys = provider._agent_field_items(
        "agent-1",
        {"id": "agent-1", "name": "a1", "type": "command", "labels": {"env": "prod"}},
        _normalized_instance({"mngr-host-id": "h"}),
    )
    assert set_tags == {
        "mngr-agent-agent-1-name": "a1",
        "mngr-agent-agent-1-type": "command",
        "mngr-agent-agent-1-labels": '{"env":"prod"}',
    }
    assert delete_keys == []


def test_agent_field_items_omits_empty_labels(temp_mngr_ctx: MngrContext) -> None:
    """An agent with absent or empty labels gets no -labels tag."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    instance = _normalized_instance({})
    for agent_data in (
        {"id": "agent-1", "name": "a1", "type": "command"},
        {"id": "agent-1", "name": "a1", "type": "command", "labels": {}},
    ):
        set_tags, _ = provider._agent_field_items("agent-1", agent_data, instance)
        assert "mngr-agent-agent-1-labels" not in set_tags


def test_agent_field_items_drops_oversized_labels_with_warning(
    temp_mngr_ctx: MngrContext, log_warnings: list[str]
) -> None:
    """Labels too large for a 256-char Azure tag are dropped (name/type kept) with a warning."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    set_tags, _ = provider._agent_field_items(
        "agent-1",
        {"id": "agent-1", "name": "a1", "type": "command", "labels": {"k": "x" * 300}},
        _normalized_instance({}),
    )
    assert set_tags == {"mngr-agent-agent-1-name": "a1", "mngr-agent-agent-1-type": "command"}
    assert any("exceeds the" in w and "labels" in w for w in log_warnings), log_warnings


def test_agent_field_items_deletes_stale_labels_on_explicit_removal(temp_mngr_ctx: MngrContext) -> None:
    """When an update carries empty labels (an explicit removal), the stale -labels tag is deleted."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    instance = _normalized_instance(
        {
            "mngr-agent-agent-1-name": "a1",
            "mngr-agent-agent-1-type": "command",
            "mngr-agent-agent-1-labels": '{"env":"prod"}',
        }
    )
    set_tags, delete_keys = provider._agent_field_items(
        "agent-1", {"id": "agent-1", "name": "a1", "type": "command", "labels": {}}, instance
    )
    assert "mngr-agent-agent-1-labels" not in set_tags
    assert delete_keys == ["mngr-agent-agent-1-labels"]


def test_agent_field_items_preserves_absent_fields_on_partial_update(temp_mngr_ctx: MngrContext) -> None:
    """A partial persist (e.g. only id+type) must NOT delete the agent's existing name/labels tags."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    instance = _normalized_instance(
        {
            "mngr-agent-agent-1-name": "a1",
            "mngr-agent-agent-1-type": "command",
            "mngr-agent-agent-1-labels": '{"env":"prod"}',
        }
    )
    set_tags, delete_keys = provider._agent_field_items("agent-1", {"id": "agent-1", "type": "claude"}, instance)
    assert set_tags == {"mngr-agent-agent-1-type": "claude"}
    assert delete_keys == []


def test_persisted_agent_dicts_reassembles_id_with_dashes(temp_mngr_ctx: MngrContext) -> None:
    """An agent id containing dashes still reassembles: the field is split off the *final* dash."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    agents = provider._persisted_agent_dicts_from_instance(
        _normalized_instance({"mngr-agent-ab-cd-ef-name": "a1", "mngr-agent-ab-cd-ef-type": "command"})
    )
    assert agents == [{"id": "ab-cd-ef", "name": "a1", "type": "command"}]


# =============================================================================
# Idle-watcher / sentinel module functions (systemd path/service + deallocate)
# =============================================================================


def test_build_idle_watcher_service_unit_execstart_points_at_deallocate_script() -> None:
    """The oneshot .service runs the host-side self-deallocate script via ExecStart."""
    unit = _build_idle_watcher_service_unit()
    assert "Type=oneshot" in unit
    assert "ExecStart=/usr/local/sbin/mngr-azure-deallocate.sh" in unit


def test_build_self_deallocate_script_fetches_token_resource_id_and_posts_deallocate() -> None:
    """The self-deallocate script fetches the IMDS token + resourceId, then POSTs the ARM deallocate.

    On Azure an OS shutdown leaves the VM Stopped-but-allocated (still billing), so
    the only in-guest way to halt compute billing is the ARM deallocate via the
    managed-identity IMDS token. The sentinel is removed first (so a resumed VM
    isn't immediately re-deallocated), and a refused deallocate (no role) just logs
    and exits non-zero -- it does NOT poweroff, since an Azure OS shutdown would not
    halt billing and would only strand the VM unreachable.
    """
    sentinel = "/mngr-btrfs/deadbeef/host_dir/commands/stop-instance-requested"
    script = _build_self_deallocate_script(sentinel)
    # IMDS managed-identity token + this VM's ARM resource id are fetched.
    assert "metadata/identity/oauth2/token" in script
    assert "metadata/instance/compute/resourceId" in script
    # The ARM deallocate POST carries an api-version.
    assert "/deallocate?api-version=" in script
    assert "-X POST" in script
    # The sentinel is removed before anything else (so resume gets a clean slate).
    assert f'rm -f "{sentinel}"' in script
    assert script.index("rm -f") < script.index("deallocate?api-version")
    # On a refused deallocate the script logs and exits -- it must NOT poweroff,
    # since an Azure OS shutdown does not halt billing.
    assert "shutdown" not in script
    assert "exit 1" in script


def test_build_self_deallocate_script_omits_sentinel_removal_for_bare() -> None:
    """With no sentinel (bare path), the script still deallocates but skips the rm line.

    The bare placement runs this directly as the agent's shutdown.sh -- there is no
    idle sentinel, so passing None omits the ``rm -f`` line while keeping the ARM
    deallocate. It still must not fall back to an OS poweroff (which would strand a
    still-billing Azure VM).
    """
    script = _build_self_deallocate_script(None)
    assert "rm -f" not in script
    assert "/deallocate?api-version=" in script
    assert "-X POST" in script
    assert "shutdown" not in script
    assert "exit 1" in script


class _RecordingReclaimClient(_SubnetStubClient):
    """Client that records reclaim_orphaned_network_resources calls."""

    reclaim_calls: list[tuple[str, bool]] = Field(default_factory=list)

    def reclaim_orphaned_network_resources(
        self, provider_name: ProviderInstanceName, dry_run: bool = False
    ) -> list[ProviderResourceInfo]:
        self.reclaim_calls.append((str(provider_name), dry_run))
        return [ProviderResourceInfo(provider_name=provider_name, kind="network_interface", name="old-nic")]


def test_gc_provider_resources_delegates_to_client(temp_mngr_ctx: MngrContext) -> None:
    """AzureProvider.gc_provider_resources forwards to the client with its own name + dry_run."""
    config = AzureProviderConfig(subscription_id="sub-123", auto_shutdown_seconds=3600)
    client = _RecordingReclaimClient(
        credential=object(),
        subscription_id="sub-123",
        region=config.default_region,
        resource_group=config.resource_group,
        vnet_name=config.vnet_name,
        subnet_name=config.subnet_name,
        nsg_name=config.nsg_name,
    )
    provider = AzureProvider(
        name=ProviderInstanceName("azure-test"),
        host_dir=config.host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=config,
        vps_client=client,
        azure_client=client,
        azure_config=config,
    )
    reclaimed = provider.gc_provider_resources(dry_run=True)
    assert client.reclaim_calls == [("azure-test", True)]
    assert [(r.kind, r.name) for r in reclaimed] == [("network_interface", "old-nic")]
