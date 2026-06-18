"""Tests for the AzureProvider's Blob-state-bucket agent-data and offline-host behavior."""

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
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
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.testing import seed_stopped_host_record

_ACCOUNT_NAME = "mngrststateacct1234"


def _build_bucket_provider(
    mngr_ctx: MngrContext, *, is_offline_host_dir_enabled: bool = True
) -> tuple[AzureProvider, FakeComputeClient]:
    """Build an AzureProvider whose ``_state_bucket`` resolves to a fake-backed Blob bucket.

    With a bucket present ``_state_store`` is the bucket-backed store, so agent and
    host records round-trip through the in-memory backend. The fake compute client
    is left with an empty VM list (the bucket is the sole offline store -- there is
    no VM tag mirror).
    """
    config = AzureProviderConfig(
        subscription_id="sub-123",
        auto_shutdown_seconds=3600,
        state_storage_account_name=_ACCOUNT_NAME,
        is_offline_host_dir_enabled=is_offline_host_dir_enabled,
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


def test_bucket_mode_persists_agent_to_bucket(temp_mngr_ctx: MngrContext) -> None:
    """With a state bucket configured, agent data round-trips through the bucket (no size limit)."""
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    seed_stopped_host_record(provider, host_id)
    big_labels = {"k": "v" * 1000}
    agent_data = {"id": str(agent_id), "name": "alpha", "type": "claude", "labels": big_labels}

    provider.persist_agent_data(host_id, agent_data)
    records = provider.list_persisted_agent_data_for_host(host_id)

    by_id = {r["id"]: r for r in records}
    assert str(agent_id) in by_id
    # A >256-char labels blob (too large for any tag) survives in the bucket.
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
    record = VpsHostRecord(certified_host_data=certified)

    provider._persist_host_record_externally(record)

    bucket = provider._state_bucket
    assert bucket is not None
    assert bucket.read_host_record_json(host_id) is not None

    # to_offline_host first tries the base SSH/volume path (no reachable VM here =>
    # HostNotFoundError), then the override reconstructs the full record from the bucket.
    offline = provider.to_offline_host(host_id)
    assert str(offline.id) == str(host_id)
    assert offline.certified_host_data.host_name == "recovered-host"


def test_to_offline_host_raises_when_bucket_record_absent(temp_mngr_ctx: MngrContext) -> None:
    """Bucket mode but no host_state.json: to_offline_host re-raises HostNotFoundError.

    The VM tag mirror was removed, so the bucket is the sole offline source: a
    host whose record is missing from the bucket cannot be reconstructed.
    """
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    # Nothing is written to the bucket for this host, so the bucket read misses and
    # the (SSH-unreachable) host cannot be reconstructed offline.
    with pytest.raises(HostNotFoundError):
        provider.to_offline_host(host_id)


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
    bucket.write_host_record_json(host_id, "{}")
    bucket.write_agent_record(host_id, "agent-1", {"id": "agent-1"})
    assert bucket.has_any_host_state() is True

    provider._delete_host_record_externally(host_id)
    assert bucket.has_any_host_state() is False


def test_no_bucket_persist_agent_data_raises_prepare_pointer_without_writing_vm_tags(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Without a resolved bucket, the agent-record mirror raises -- and writes no VM tag patch.

    The VM tag mirror was removed and the bucket is required: with no bucket,
    ``_state_store`` raises the actionable prepare-pointer error, so the write fails
    loudly. Pre-seeding ``_state_bucket`` with None must NOT take the persist down
    any tag path -- the fake resource client records no Merge patch before the raise.
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
    with pytest.raises(MngrError, match="mngr azure prepare"):
        provider.persist_agent_data(host_id, {"id": str(agent_id), "name": "alpha", "type": "claude"})
    # No tag path: no server-side tag Merge patch was recorded.
    assert resource.tags.updates == []


# =============================================================================
# Offline host_dir volume (get_volume_for_host / get_volume_reference_for_host)
# =============================================================================


def test_get_volume_reference_is_cheap_and_scoped_to_host_dir(temp_mngr_ctx: MngrContext) -> None:
    """The reference getter returns a host_dir-scoped volume with no probe."""
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    # Seed under the host's host_dir prefix via the bucket's own volume writer.
    bucket.volume_for_host(host_id).write_files({"events/e.jsonl": b"evt"})
    reference = provider.get_volume_reference_for_host(host_id)
    assert reference is not None
    assert reference.volume.read_file("events/e.jsonl") == b"evt"


def test_get_volume_for_host_returns_volume_when_objects_present(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    bucket = provider._state_bucket
    assert bucket is not None
    bucket.volume_for_host(host_id).write_files({"logs/a.log": b"a"})
    volume = provider.get_volume_for_host(host_id)
    assert volume is not None
    assert volume.volume.read_file("logs/a.log") == b"a"


def test_get_volume_for_host_returns_none_when_prefix_empty(temp_mngr_ctx: MngrContext) -> None:
    """An empty host_dir prefix yields None -- nothing was captured to the bucket yet.

    With operator-driven host_dir, an empty prefix just means the host was never
    `mngr stop`-ped (or idle-self-poweroffed with no operator to capture it); the
    read has no volume to serve, with no VM probe or raise.
    """
    provider, _compute = _build_bucket_provider(temp_mngr_ctx)
    assert provider.get_volume_for_host(HostId.generate()) is None


def test_get_volume_reference_is_none_when_feature_disabled(temp_mngr_ctx: MngrContext) -> None:
    provider, _compute = _build_bucket_provider(temp_mngr_ctx, is_offline_host_dir_enabled=False)
    assert provider.get_volume_reference_for_host(HostId.generate()) is None
    assert provider.get_volume_for_host(HostId.generate()) is None
