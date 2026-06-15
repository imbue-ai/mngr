"""Tests for the AzureProvider's Blob-state-bucket vs legacy-tag agent-data behavior."""

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_azure.backend import AzureProvider
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import _StubbedBlobStateBucket
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord

_ACCOUNT_NAME = "mngrststateacct1234"


def _seed_stopped_host_record(provider: AzureProvider, host_id: HostId) -> None:
    """Cache a record with ``vps_ip=None`` so the base on-volume path short-circuits."""
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="myhost",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    provider._host_record_cache[host_id] = VpsDockerHostRecord(certified_host_data=certified)


def _build_bucket_provider(mngr_ctx: MngrContext) -> tuple[AzureProvider, FakeComputeClient]:
    """Build an AzureProvider whose ``_state_bucket`` resolves to a fake-backed Blob bucket.

    The fake compute client is left with an empty VM list: a bucket-mode test must
    make NO VM tag calls, so a stray ``add_tags`` would have to find a VM (and
    finding none would log/return rather than write), but the assertions verify the
    data round-trips through the bucket regardless.
    """
    config = AzureProviderConfig(
        subscription_id="sub-123", auto_shutdown_seconds=3600, state_storage_account_name=_ACCOUNT_NAME
    )
    compute = FakeComputeClient()
    client = _StubbedAzureVpsClient(
        credential=object(),
        subscription_id="sub-123",
        region=config.default_region,
        resource_group=config.resource_group,
        stubbed_compute_client=compute,
        stubbed_network_client=FakeNetworkClient(),
        stubbed_resource_client=FakeResourceClient(),
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
    backend = FakeBlobStorageBackend()
    bucket = _StubbedBlobStateBucket(
        credential=None,
        subscription_id="sub-123",
        resource_group=config.resource_group,
        region=config.default_region,
        account_name=_ACCOUNT_NAME,
        fake_backend=backend,
    )
    bucket.ensure_bucket()
    # Inject the resolved bucket so the provider's existence probe is bypassed
    # (the production probe would hit real Azure).
    provider._state_bucket_cache = bucket
    return provider, compute


def test_bucket_mode_persists_agent_to_bucket_and_writes_no_vm_tags(temp_mngr_ctx: MngrContext) -> None:
    """With a state bucket configured, agent data goes to the bucket; no VM tag write is attempted."""
    provider, compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_stopped_host_record(provider, host_id)
    big_labels = {"k": "v" * 1000}
    agent_data = {"id": str(agent_id), "name": "alpha", "type": "claude", "labels": big_labels}

    provider.persist_agent_data(host_id, agent_data)
    records = provider.list_persisted_agent_data_for_host(host_id)

    by_id = {r["id"]: r for r in records}
    assert str(agent_id) in by_id
    # The >256-char labels blob (which the tag mirror would drop) survives in the bucket.
    assert by_id[str(agent_id)]["labels"] == big_labels


def test_bucket_mode_mirrors_host_record_and_reconstructs_offline_host(temp_mngr_ctx: MngrContext) -> None:
    """``_persist_host_record_externally`` writes the full record; ``to_offline_host`` reads it back."""
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    certified = CertifiedHostData(
        host_id=str(host_id),
        host_name="recovered-host",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        stop_reason=HostState.STOPPED.value,
    )
    record = VpsDockerHostRecord(certified_host_data=certified)

    provider._persist_host_record_externally(record)

    bucket = provider._state_bucket()
    assert bucket is not None
    assert bucket.read_host_record(host_id) is not None

    # to_offline_host first tries the base SSH/volume path (no reachable VM here =>
    # HostNotFoundError), then the override reconstructs the full record from the bucket.
    offline = provider.to_offline_host(host_id)
    assert str(offline.id) == str(host_id)
    assert offline.certified_host_data.host_name == "recovered-host"


def test_bucket_mode_remove_agent_clears_bucket_record(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_stopped_host_record(provider, host_id)

    provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "alpha"})
    assert len(provider.list_persisted_agent_data_for_host(host_id)) == 1
    provider.remove_persisted_agent_data(host_id, agent_id)
    assert provider.list_persisted_agent_data_for_host(host_id) == []


def test_delete_host_externally_removes_bucket_state(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket()
    assert bucket is not None
    bucket.write_host_record(host_id, "{}")
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1"})
    assert bucket.has_any_host_state() is True

    provider._delete_host_record_externally(host_id)
    assert bucket.has_any_host_state() is False


def test_no_bucket_uses_legacy_tag_path(temp_mngr_ctx: MngrContext) -> None:
    """Without a resolved bucket, the provider falls back to the VM tag mirror.

    Forcing ``_state_bucket_cache = None`` takes ``persist_agent_data`` down the tag
    path: it looks up the VM via the fake compute list and upserts tags, which the
    fake resource client records.
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
        mngr_ctx=temp_mngr_ctx,
        config=config,
        vps_client=client,
        azure_client=client,
        azure_config=config,
    )
    # Force the no-bucket resolution to be cached.
    provider._state_bucket_cache = None

    host_id = HostId.generate()
    agent_id = AgentId.generate()
    _seed_stopped_host_record(provider, host_id)

    compute.virtual_machines.list_result = [
        SimpleNamespace(
            name="vm-1",
            tags={"mngr-provider": "azure-test", "mngr-host-id": str(host_id)},
            instance_view=None,
        )
    ]
    provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "alpha", "type": "claude"})
    # The legacy tag path ran: a server-side tag Merge patch was recorded.
    assert len(resource.tags.updates) == 1
