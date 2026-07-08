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
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import ProviderResourceInfo
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.backend import AzureProvider
from imbue.mngr_azure.backend import AzureProviderBackend
from imbue.mngr_azure.backend import ParsedAzureBuildOptions
from imbue.mngr_azure.backend import _build_idle_watcher_service_unit
from imbue.mngr_azure.backend import _build_self_deallocate_script
from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import _StubbedBlobStateBucket
from imbue.mngr_vps.host_state_store import BucketHostStateStore
from imbue.mngr_vps.host_store import VpsHostConfig
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.primitives import VpsInstanceId
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
# Offline paths (stop/start lifecycle): instance lookup, offline discovery from
# the cheap identity tags, and the no-bucket state-store behavior.
#
# These build an AzureProvider over a _StubbedAzureVpsClient whose fake compute
# client returns hand-built VM SimpleNamespaces; the provider normalizes them
# through the real list_instances path (tags -> "key=value" list, power state ->
# "state"). The Azure analog of the AWS/GCP backend offline tests.
#
# The per-agent VM tag mirror was removed: agent records and the full host record
# now live solely in the Blob state bucket (via ``_state_store``). The base
# identity tags (``mngr-host-id`` / ``mngr-host-name``) are still stamped at
# create and drive offline discovery of a deallocated VM.
# =============================================================================


def _build_stubbed_provider(
    mngr_ctx: MngrContext,
) -> tuple[AzureProvider, FakeComputeClient, FakeResourceClient]:
    """Build an AzureProvider whose Azure client is a _StubbedAzureVpsClient over fakes.

    Returns the provider, the fake compute client (so a test can seed
    ``virtual_machines.list_result`` -- the host-id lookup and offline discovery
    read it) and the fake resource client. No ``_state_bucket`` is seeded, so the
    provider's ``_state_store`` raises the actionable missing-bucket error.
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
    # No state bucket: seed the cached_property to None so ``_state_store`` raises
    # the actionable missing-bucket error (the bucket is required) and the existence
    # probe never hits real Azure.
    provider.__dict__["_state_bucket"] = None
    return provider, compute, resource


def _seed_fake_bucket(provider: AzureProvider) -> _StubbedBlobStateBucket:
    """Seed a fake-backed Blob state bucket into ``provider._state_bucket`` and return it.

    Makes ``_state_store`` resolve to a ``BucketHostStateStore`` so offline reads
    (agent records, the host record) round-trip through the in-memory backend rather
    than raising the missing-bucket error.
    """
    config = provider.azure_config
    bucket = _StubbedBlobStateBucket(
        credential=None,
        subscription_id="sub-123",
        resource_group=config.resource_group,
        region=config.default_region,
        account_name="mngrststateacct1234",
        fake_backend=FakeBlobStorageBackend(),
    )
    bucket.ensure_bucket()
    provider.__dict__["_state_bucket"] = bucket
    return bucket


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
    (and the offline operations keyed off this lookup) onto the wrong VM. The lookup
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


def test_no_bucket_persist_agent_data_raises_prepare_pointer(temp_mngr_ctx: MngrContext) -> None:
    """With no state bucket, mirroring an agent record raises the prepare-pointer error.

    The per-agent VM tag mirror is gone and the bucket is required: ``_state_store``
    raises the actionable missing-bucket error, so even a write fails loudly rather
    than silently dropping the mirror. The seeded ``vps_ip=None`` record makes
    ``super().persist_agent_data`` short-circuit with ``HostNotFoundError``, so the
    raise comes from the offline mirror step. No server-side tag patch is recorded.
    """
    provider, _compute, resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    with pytest.raises(MngrError, match="mngr azure prepare"):
        provider.persist_agent_data(
            host_id,
            {"id": str(agent_id), "name": "a1", "type": "command", "labels": {"env": "prod"}},
        )
    assert resource.tags.updates == []


def test_no_bucket_list_persisted_agent_data_raises_prepare_pointer(temp_mngr_ctx: MngrContext) -> None:
    """An offline agent-record read against a no-bucket provider raises the prepare-pointer error."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    seed_stopped_host_record(provider, host_id)
    with pytest.raises(MngrError, match="mngr azure prepare"):
        provider.list_persisted_agent_data_for_host(host_id)


def test_no_bucket_to_offline_host_raises_prepare_pointer(temp_mngr_ctx: MngrContext) -> None:
    """A stopped host cannot be reconstructed without a bucket: ``to_offline_host`` raises a prepare pointer."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    with pytest.raises(MngrError, match="mngr azure prepare"):
        provider.to_offline_host(host_id)


def test_no_bucket_discovery_of_deallocated_vm_raises_prepare_pointer(temp_mngr_ctx: MngrContext) -> None:
    """A deallocated VM surfaced in discovery reads its agents from the store, which raises without a bucket.

    The cheap identity tags still identify the deallocated VM, so the offline
    discovery loop reaches the agent-record read -- and that read against the
    bucket-less ``_state_store`` raises the actionable prepare-pointer error
    (rather than silently dropping the stopped host's agents).
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
    _set_power_state(compute, "deallocated")
    with ConcurrencyGroup(name="test") as cg:
        with pytest.raises(MngrError, match="mngr azure prepare"):
            provider.discover_hosts_and_agents(cg)


def test_offline_discovered_host_from_instance_builds_stopped_host(temp_mngr_ctx: MngrContext) -> None:
    """A deallocated VM with mngr-host-id + mngr-host-name tags yields a STOPPED DiscoveredHost.

    Exercises the shared ``OfflineCapableVpsProvider._offline_discovered_host_from_instance``
    default through Azure's ``_host_name_tag_key()`` hook (``mngr-host-name``). Reads
    only the cheap identity tags (never the bucket), so this works regardless of
    whether a state bucket exists.
    """
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    instance = {
        "id": "vm-1",
        "tags": [f"mngr-host-id={host_id}", "mngr-host-name=mngr-myhost", "mngr-provider=azure-test"],
    }
    discovered = provider._offline_discovered_host_from_instance(instance)
    assert discovered is not None
    assert discovered.host_id == host_id
    assert str(discovered.host_name) == "myhost"
    assert discovered.host_state == HostState.STOPPED
    assert discovered.provider_name == provider.name


def test_offline_discovered_host_from_instance_returns_none_without_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """A VM with no mngr-host-id tag is not a mngr host: the discovery helper returns None."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    instance = {"id": "vm-1", "tags": ["mngr-provider=azure-test"]}
    assert provider._offline_discovered_host_from_instance(instance) is None


def test_discover_hosts_and_agents_surfaces_deallocated_host_with_bucket_agents(temp_mngr_ctx: MngrContext) -> None:
    """A deallocated VM surfaces as a STOPPED host, with its agents read from the state bucket.

    The cheap identity tags (host id + name) identify the deallocated VM; its agent
    records come from the Blob bucket via ``_state_store``, not from VM tags.
    """
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    bucket = _seed_fake_bucket(provider)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    bucket.write_agent_record(host_id, str(agent_id), {"id": str(agent_id), "name": "a1", "type": "command"})
    _seed_compute(
        compute,
        [
            _vm(
                "vm-1",
                tags={"mngr-host-id": str(host_id), "mngr-provider": "azure-test", "mngr-host-name": "mngr-myhost"},
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
    assert [a.agent_id for a in agents] == [agent_id]
    assert str(agents[0].agent_name) == "a1"


def test_discover_hosts_and_agents_skips_vm_with_absent_host_id_tag(temp_mngr_ctx: MngrContext) -> None:
    """A VM with no mngr-host-id tag is skipped; a well-formed deallocated host still surfaces.

    Bucket-mode: the good host's agents read empty from the bucket (none written),
    so it surfaces with no agents while the bad VM never enters the result.
    """
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    _seed_fake_bucket(provider)
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


def test_to_offline_host_reconstructs_stopped_host_from_bucket(temp_mngr_ctx: MngrContext) -> None:
    """to_offline_host rebuilds a STOPPED offline host from the bucket's host record when SSH can't reach it.

    The full record is read from ``_state_store`` (the Blob bucket), not from VM
    tags -- the tag-based reconstruction was removed.
    """
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    bucket = _seed_fake_bucket(provider)
    host_id = HostId.generate()
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="recovered-host",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    bucket.write_host_record_json(host_id, VpsHostRecord(certified_host_data=certified).model_dump_json())

    offline = provider.to_offline_host(host_id)
    assert offline.id == host_id
    assert str(offline.get_certified_data().host_name) == "recovered-host"
    assert offline.get_state() == HostState.STOPPED


def test_remirror_host_name_restamps_tag_preserving_other_tags(temp_mngr_ctx: MngrContext) -> None:
    """A rename re-stamps the mngr-host-name VM tag, merging into (not replacing) the VM's other tags.

    Offline discovery reads the host name off this cheap index tag, so without the
    re-stamp a renamed-then-stopped host lists under its old name. The merge is the
    key correctness property: the other index tags (mngr-host-id, mngr-provider)
    must survive Azure's whole-dict tag replacement.
    """
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    compute.virtual_machines.get_result = SimpleNamespace(
        tags={"mngr-host-id": "host-1", "mngr-provider": "azure-test", "mngr-host-name": "mngr-old"}
    )
    host_id = HostId.generate()
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    record = VpsHostRecord(
        certified_host_data=certified,
        config=VpsHostConfig(vps_instance_id=VpsInstanceId("vm1"), region="westus", plan="Standard_B2s"),
    )

    provider._remirror_host_name(record, HostName("new"))

    assert len(compute.virtual_machines.updated) == 1
    vm_name, parameters = compute.virtual_machines.updated[0]
    assert vm_name == "vm1"
    assert parameters.tags == {
        "mngr-host-id": "host-1",
        "mngr-provider": "azure-test",
        "mngr-host-name": "mngr-new",
    }


def test_remirror_host_name_is_noop_without_config(temp_mngr_ctx: MngrContext) -> None:
    """A record without VPS config (no instance id) skips the re-stamp rather than erroring."""
    provider, compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    certified = CertifiedHostData(
        host_id=str(HostId.generate()),
        host_name="old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    provider._remirror_host_name(VpsHostRecord(certified_host_data=certified), HostName("new"))

    assert compute.virtual_machines.updated == []


# =============================================================================
# State store selection (BucketHostStateStore, or raise when the bucket is absent)
# =============================================================================


def test_state_store_raises_prepare_pointer_when_no_bucket(temp_mngr_ctx: MngrContext) -> None:
    """With no resolved bucket, accessing ``_state_store`` raises the actionable prepare-pointer error."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    with pytest.raises(MngrError, match="mngr azure prepare"):
        _ = provider._state_store


def test_state_store_is_bucket_store_when_bucket_exists(temp_mngr_ctx: MngrContext) -> None:
    """When a bucket exists, ``_state_store`` is the bucket-backed store over that bucket."""
    provider, _compute, _resource = _build_stubbed_provider(temp_mngr_ctx)
    bucket = _seed_fake_bucket(provider)
    store = provider._state_store
    assert isinstance(store, BucketHostStateStore)
    assert store.bucket is bucket


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
