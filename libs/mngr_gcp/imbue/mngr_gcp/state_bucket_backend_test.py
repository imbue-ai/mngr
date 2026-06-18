"""Tests for the GcpProvider's GCS-state-bucket offline ``host_dir`` integration.

The GCP provider keeps host + agent *records* in GCE instance metadata (see
``_GceMetadataHostStateStore``) -- the GCS state bucket is used specifically
for the offline ``host_dir`` mirror (the size-bounded blob whose limit metadata
would hit). These tests cover the bucket-backed ``_host_dir_backend`` paths;
record-store coverage stays in ``backend_test.py`` (metadata-backed).
"""

from typing import Any

from google.auth.credentials import AnonymousCredentials
from google.auth.credentials import Credentials
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_gcp.backend import GCP_BACKEND_NAME
from imbue.mngr_gcp.backend import GcpProvider
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_gcp.testing import FakeFirewallsClient
from imbue.mngr_gcp.testing import FakeInstancesClient
from imbue.mngr_gcp.testing import _FAKE_CREDENTIALS
from imbue.mngr_gcp.testing import _FakeStorageClient
from imbue.mngr_gcp.testing import _StubbedGcpVpsClient
from imbue.mngr_gcp.testing import _StubbedGcsStateBucket
from imbue.mngr_vps.host_state_store import NullHostDirBackend
from imbue.mngr_vps.instance_offline import BucketHostDirBackend

_BUCKET_NAME = "mngr-state-test-project"


class _StubBucketConfig(GcpProviderConfig):
    """``GcpProviderConfig`` that builds a stubbed (fake-GCS) state bucket.

    Overrides ``build_state_bucket`` to construct a ``_StubbedGcsStateBucket``
    (with the fake client injected through ``stubbed_storage_client``) instead
    of a real ``GcsStateBucket`` -- the cheap test-only seam mirroring the
    ``_StubbedGcpVpsClient`` pattern.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Typed ``Any`` because ``_FakeStorageClient`` is a plain class without a
    # pydantic core schema; the config carries it for ``build_state_bucket`` to
    # forward into the stubbed bucket.
    stubbed_storage_client: Any = Field(
        default_factory=_FakeStorageClient, description="Fake GCS client backing this config's state bucket"
    )

    def build_state_bucket(self, credentials: Credentials, project_id: str, region: str) -> GcsStateBucket:
        return _StubbedGcsStateBucket(
            credentials=credentials,
            project_id=project_id,
            region=region,
            bucket_name=self.resolve_state_bucket_name(project_id),
            stubbed_storage_client=self.stubbed_storage_client,
        )


def _build_bucket_provider(
    mngr_ctx: MngrContext,
    *,
    is_offline_host_dir_enabled: bool = True,
    bucket_present: bool = True,
) -> tuple[GcpProvider, _FakeStorageClient]:
    """Build a GcpProvider whose ``_state_bucket`` is the fake GCS bucket.

    ``bucket_present`` toggles whether the bucket already exists in the fake (so
    the production ``_resolve_existing_state_bucket`` returns the bucket vs None).
    Returns the provider and the underlying fake client so tests can seed objects
    directly.
    """
    fake = _FakeStorageClient()
    config = _StubBucketConfig(
        backend=GCP_BACKEND_NAME,
        project_id="test-project",
        default_zone="us-west1-a",
        auto_shutdown_seconds=3600,
        is_offline_host_dir_enabled=is_offline_host_dir_enabled,
        stubbed_storage_client=fake,
    )
    if bucket_present:
        _StubbedGcsStateBucket(
            credentials=_FAKE_CREDENTIALS,
            project_id="test-project",
            region="us-west1",
            bucket_name=_BUCKET_NAME,
            stubbed_storage_client=fake,
        ).ensure_bucket()
    instances = FakeInstancesClient()
    firewalls = FakeFirewallsClient()
    client = _StubbedGcpVpsClient(
        credentials=AnonymousCredentials(),
        project_id="test-project",
        zone="us-west1-a",
        image=config.default_source_image,
        auto_shutdown_seconds=3600,
        stubbed_instances_client=instances,
        stubbed_firewalls_client=firewalls,
    )
    provider = GcpProvider(
        name=ProviderInstanceName("gcp-test"),
        host_dir=config.host_dir,
        mngr_ctx=mngr_ctx,
        config=config,
        vps_client=client,
        gcp_client=client,
        gcp_config=config,
    )
    return provider, fake


def test_state_bucket_is_none_when_not_yet_created(temp_mngr_ctx: MngrContext) -> None:
    """``_resolve_existing_state_bucket`` returns None when the bucket has not been provisioned."""
    provider, _fake = _build_bucket_provider(temp_mngr_ctx, bucket_present=False)
    assert provider._state_bucket is None


def test_state_bucket_is_resolved_when_present(temp_mngr_ctx: MngrContext) -> None:
    provider, _fake = _build_bucket_provider(temp_mngr_ctx)
    assert isinstance(provider._state_bucket, GcsStateBucket)


def test_host_dir_backend_is_bucket_backed_when_enabled_and_present(temp_mngr_ctx: MngrContext) -> None:
    """With the feature on and the bucket created, the host_dir backend is bucket-backed."""
    provider, _fake = _build_bucket_provider(temp_mngr_ctx)
    assert isinstance(provider._host_dir_backend, BucketHostDirBackend)


def test_host_dir_backend_is_null_when_feature_disabled(temp_mngr_ctx: MngrContext) -> None:
    """``is_offline_host_dir_enabled=False`` keeps the backend as the no-op fallback."""
    provider, _fake = _build_bucket_provider(temp_mngr_ctx, is_offline_host_dir_enabled=False)
    assert isinstance(provider._host_dir_backend, NullHostDirBackend)


def test_host_dir_backend_is_null_when_bucket_absent(temp_mngr_ctx: MngrContext) -> None:
    """A missing bucket also degrades to the no-op fallback (offline host_dir simply unavailable)."""
    provider, _fake = _build_bucket_provider(temp_mngr_ctx, bucket_present=False)
    assert isinstance(provider._host_dir_backend, NullHostDirBackend)


def test_get_volume_reference_reads_files_from_host_dir_prefix(temp_mngr_ctx: MngrContext) -> None:
    """The reference getter returns a host_dir-scoped volume that reads seeded files back."""
    provider, fake = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    # Seed a file under the host's host_dir prefix and confirm the reference reads it.
    fake.buckets[_BUCKET_NAME].blob(f"hosts/{hex_id}/host_dir/events/e.jsonl").upload_from_string(b"evt")
    reference = provider.get_volume_reference_for_host(host_id)
    assert reference is not None
    assert reference.volume.read_file("events/e.jsonl") == b"evt"


def test_get_volume_for_host_returns_none_when_prefix_empty(temp_mngr_ctx: MngrContext) -> None:
    """An empty host_dir prefix yields None -- nothing was captured to the bucket yet.

    With operator-driven host_dir, an empty prefix just means the host was never
    ``mngr stop``-ped (or idle-self-poweroffed with no operator to capture it); the
    read simply has no volume to serve, with no instance probe or raise.
    """
    provider, _fake = _build_bucket_provider(temp_mngr_ctx)
    assert provider.get_volume_for_host(HostId.generate()) is None


def test_get_volume_for_host_returns_volume_when_objects_present(temp_mngr_ctx: MngrContext) -> None:
    """A non-empty host_dir prefix yields a readable volume."""
    provider, fake = _build_bucket_provider(temp_mngr_ctx)
    host_id = HostId.generate()
    hex_id = host_id.get_uuid().hex
    fake.buckets[_BUCKET_NAME].blob(f"hosts/{hex_id}/host_dir/logs/a.log").upload_from_string(b"a")
    volume = provider.get_volume_for_host(host_id)
    assert volume is not None
    assert volume.volume.read_file("logs/a.log") == b"a"


def test_get_volume_reference_is_none_when_feature_disabled(temp_mngr_ctx: MngrContext) -> None:
    """With ``is_offline_host_dir_enabled=False``, no offline host_dir volume is returned."""
    provider, _fake = _build_bucket_provider(temp_mngr_ctx, is_offline_host_dir_enabled=False)
    assert provider.get_volume_reference_for_host(HostId.generate()) is None
    assert provider.get_volume_for_host(HostId.generate()) is None
