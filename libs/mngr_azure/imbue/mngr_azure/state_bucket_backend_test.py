"""Tests for the AzureProvider's Blob-state-bucket vs tag agent-data behavior."""

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import capture_log_warnings
from imbue.mngr_azure.backend import AzureProvider
from imbue.mngr_azure.config import AzureProviderConfig
from imbue.mngr_azure.state_bucket import BlobStateHostIdentity
from imbue.mngr_azure.testing import FakeAuthorizationClient
from imbue.mngr_azure.testing import FakeBlobStorageBackend
from imbue.mngr_azure.testing import FakeComputeClient
from imbue.mngr_azure.testing import FakeManagedServiceIdentityClient
from imbue.mngr_azure.testing import FakeNetworkClient
from imbue.mngr_azure.testing import FakeResourceClient
from imbue.mngr_azure.testing import _StubbedAzureVpsClient
from imbue.mngr_azure.testing import _StubbedBlobStateBucket
from imbue.mngr_azure.testing import _StubbedBlobStateHostIdentity
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.testing import seed_stopped_host_record

_ACCOUNT_NAME = "mngrststateacct1234"


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
    # Pre-seed the ``_state_bucket`` cached_property so the provider's existence
    # probe is bypassed (the production probe would hit real Azure).
    provider.__dict__["_state_bucket"] = bucket
    return provider, compute


def test_bucket_mode_persists_agent_to_bucket_and_writes_no_vm_tags(temp_mngr_ctx: MngrContext) -> None:
    """With a state bucket configured, agent data goes to the bucket; no VM tag write is attempted."""
    provider, compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
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

    bucket = provider._state_bucket
    assert bucket is not None
    assert bucket.read_host_record(host_id) is not None

    # to_offline_host first tries the base SSH/volume path (no reachable VM here =>
    # HostNotFoundError), then the override reconstructs the full record from the bucket.
    offline = provider.to_offline_host(host_id)
    assert str(offline.id) == str(host_id)
    assert offline.certified_host_data.host_name == "recovered-host"


def test_to_offline_host_falls_back_to_tags_when_bucket_record_absent(temp_mngr_ctx: MngrContext) -> None:
    """Bucket mode but no host_state.json yet: to_offline_host reconstructs from the VM's own tags.

    Covers ``read_host_record_with_tag_fallback`` -- a bucket-mode host created
    before the bucket existed has no ``host_state.json``, so the offline
    reconstruction must fall back to the VM tag mirror rather than 404.
    """
    provider, compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    # Nothing is written to the bucket for this host, so the bucket read misses
    # and the tag fallback (reconstruction from the VM's own tags) runs.
    compute.virtual_machines.list_result = [
        SimpleNamespace(
            name="vm-1",
            tags={
                "mngr-provider": "azure-test",
                "mngr-host-id": str(host_id),
                "mngr-host-name": "mngr-myhost",
                "mngr-created-at": "2026-01-01T00:00:00+00:00",
            },
            instance_view=None,
        )
    ]
    offline = provider.to_offline_host(host_id)
    assert offline.id == host_id
    assert str(offline.get_certified_data().host_name) == "myhost"


def test_bucket_mode_remove_agent_clears_bucket_record(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)

    provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "alpha"})
    assert len(provider.list_persisted_agent_data_for_host(host_id)) == 1
    provider.remove_persisted_agent_data(host_id, agent_id)
    assert provider.list_persisted_agent_data_for_host(host_id) == []


def test_delete_host_externally_removes_bucket_state(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    bucket.write_host_record(host_id, "{}")
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1"})
    assert bucket.has_any_host_state() is True

    provider._delete_host_record_externally(host_id)
    assert bucket.has_any_host_state() is False


def test_no_bucket_uses_tag_path(temp_mngr_ctx: MngrContext) -> None:
    """Without a resolved bucket, the provider falls back to the VM tag mirror.

    Pre-seeding ``_state_bucket`` with None takes ``persist_agent_data`` down the
    tag path: it looks up the VM via the fake compute list and upserts tags, which
    the fake resource client records.
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
    # Pre-seed the no-bucket resolution into the cached_property.
    provider.__dict__["_state_bucket"] = None

    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)

    compute.virtual_machines.list_result = [
        SimpleNamespace(
            name="vm-1",
            tags={"mngr-provider": "azure-test", "mngr-host-id": str(host_id)},
            instance_view=None,
        )
    ]
    provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "alpha", "type": "claude"})
    # The tag path ran: a server-side tag Merge patch was recorded.
    assert len(resource.tags.updates) == 1


# =============================================================================
# Offline host_dir volume (get_volume_for_host / get_volume_reference_for_host)
# =============================================================================


class _IdentityInjectingAzureProvider(AzureProvider):
    """AzureProvider whose ``_host_identity`` returns a test-injected stubbed identity."""

    injected_host_identity: BlobStateHostIdentity | None = None

    def _host_identity(self) -> BlobStateHostIdentity | None:
        return self.injected_host_identity


def _build_provider_with_identity(
    mngr_ctx: MngrContext,
    *,
    is_offline_host_dir_enabled: bool = True,
    identity_exists: bool = True,
    compute: FakeComputeClient | None = None,
) -> tuple[_IdentityInjectingAzureProvider, FakeComputeClient]:
    """Build a bucket-mode provider with a fake-backed (optionally provisioned) host identity."""
    config = AzureProviderConfig(
        subscription_id="sub-123",
        auto_shutdown_seconds=3600,
        state_storage_account_name=_ACCOUNT_NAME,
        is_offline_host_dir_enabled=is_offline_host_dir_enabled,
    )
    compute = compute or FakeComputeClient()
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
    msi = FakeManagedServiceIdentityClient()
    identity = _StubbedBlobStateHostIdentity(
        credential=None,
        subscription_id="sub-123",
        resource_group=config.resource_group,
        region=config.default_region,
        account_name=_ACCOUNT_NAME,
        fake_msi_client=msi,
        fake_authorization_client=FakeAuthorizationClient(),
    )
    if identity_exists:
        identity.ensure_host_identity()
    provider = _IdentityInjectingAzureProvider(
        name=ProviderInstanceName("azure-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        azure_client=client,
        azure_config=config,
        injected_host_identity=identity,
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
    provider.__dict__["_state_bucket"] = bucket
    return provider, compute


def test_get_volume_reference_is_cheap_and_scoped_to_host_dir(temp_mngr_ctx: MngrContext) -> None:
    """The reference getter returns a host_dir-scoped volume with no probe."""
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    # Seed under the host's host_dir prefix via the bucket's own volume writer.
    bucket.volume_for_host(host_id).write_files({"events/e.jsonl": b"evt"})
    reference = provider.get_volume_reference_for_host(host_id)
    assert reference is not None
    assert reference.volume.read_file("events/e.jsonl") == b"evt"


def test_get_volume_for_host_returns_volume_when_objects_present(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    bucket.volume_for_host(host_id).write_files({"logs/a.log": b"a"})
    volume = provider.get_volume_for_host(host_id)
    assert volume is not None
    assert volume.volume.read_file("logs/a.log") == b"a"


def test_get_volume_for_host_returns_none_when_prefix_empty(temp_mngr_ctx: MngrContext) -> None:
    """An empty host_dir prefix yields None (the diagnostic runs, non-fatally)."""
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx)
    host_id = HostId.generate()
    # No VM matches the host id, so the diagnostic returns early (no identity probe).
    assert provider.get_volume_for_host(host_id) is None


def test_get_volume_for_host_warns_when_vm_has_no_managed_identity(temp_mngr_ctx: MngrContext) -> None:
    """Empty host_dir + a VM with no user-assigned identity -> a 're-run prepare' WARNING (non-fatal)."""
    compute = FakeComputeClient()
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx, compute=compute)
    host_id = HostId.generate()
    # A matching VM with NO user-assigned identity (only system-assigned).
    compute.virtual_machines.list_result = [
        SimpleNamespace(name="vm-1", tags={"mngr-provider": "azure-test", "mngr-host-id": str(host_id)})
    ]
    compute.virtual_machines.get_result = SimpleNamespace(identity=SimpleNamespace(user_assigned_identities=None))
    with capture_log_warnings() as warnings:
        assert provider.get_volume_for_host(host_id) is None
    assert any("no attached user-assigned managed identity" in message for message in warnings)


def test_get_volume_reference_is_none_when_feature_disabled(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx, is_offline_host_dir_enabled=False)
    assert provider.get_volume_reference_for_host(HostId.generate()) is None
    assert provider.get_volume_for_host(HostId.generate()) is None


def test_host_dir_sync_identity_resource_id_returned_when_identity_exists(temp_mngr_ctx: MngrContext) -> None:
    """The create path attaches the provisioned identity's resource id when it exists."""
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx, identity_exists=True)
    resource_id = provider._host_dir_sync_identity_resource_id()
    assert resource_id is not None
    assert resource_id.endswith(f"/userAssignedIdentities/mngrid-{_ACCOUNT_NAME}")


def test_host_dir_sync_identity_resource_id_none_when_identity_absent(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_provider_with_identity(temp_mngr_ctx, identity_exists=False)
    assert provider._host_dir_sync_identity_resource_id() is None


def test_host_dir_sync_identity_resource_id_none_when_feature_disabled(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_provider_with_identity(
        temp_mngr_ctx, is_offline_host_dir_enabled=False, identity_exists=True
    )
    assert provider._host_dir_sync_identity_resource_id() is None
