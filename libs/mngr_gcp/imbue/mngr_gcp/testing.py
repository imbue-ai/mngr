"""Shared test helpers and constants for mngr_gcp.

Lives outside ``conftest.py`` so other test modules (e.g. ``test_release_gcp``)
can import these directly; importing from a ``conftest.py`` is a pytest
anti-pattern (those files are auto-discovered, not designed for direct import).
Mirrors ``libs/mngr_aws/imbue/mngr_aws/testing.py``.
"""

import os
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final

from google.api_core import exceptions as google_api_exceptions
from google.auth import exceptions as google_auth_exceptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import compute_v1
from pydantic import Field

from imbue.mngr.interfaces.volume import Volume
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_gcp.errors import GcpCredentialsError
from imbue.mngr_gcp.errors import GcpProjectError
from imbue.mngr_gcp.state_bucket import GcsStateBucket
from imbue.mngr_gcp.state_bucket import GcsVolume

# Prefix release tests give their agent names so a leaked instance (should the
# scanner ever fail) is identifiable as mngr-created. Cleanup does not depend on
# it: ``create_instance`` labels pytest instances ``mngr-pytest-launched=true``
# and the conftest scanner filters on that label.
GCP_TEST_NAME_PREFIX: Final[str] = "mngr-test-"

# Zone used by the GCP release tests and the session-end leak scan, overridable
# via ``MNGR_GCP_ZONE``. Read once at import so conftest and test_release_gcp
# observe the same value.
GCP_DEFAULT_ZONE: Final[str] = os.environ.get("MNGR_GCP_ZONE", "us-west1-a")

# Region derived from the zone for the firewall / settings.toml.
GCP_DEFAULT_REGION: Final[str] = GCP_DEFAULT_ZONE.rsplit("-", 1)[0]

# Release-test opt-in flag. Mirrors the gate that ``test_release_gcp.py`` uses
# on ``pytestmark`` and that ``conftest.py`` uses to suppress the session-end
# orphan scan when no release tests were requested.
GCP_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_GCP_RELEASE_TESTS") == "1"

# Single source of truth for the release-test instance lifetime, kept aligned
# across two consumers:
#   1. ``test_release_gcp.py`` writes it into a tmp-path settings.toml
#      (``[providers.gcp] auto_shutdown_seconds``) so each instance launches
#      with scheduling.max_run_duration + instance_termination_action=DELETE.
#   2. ``conftest.py`` derives the orphan-scan grace period from it so the
#      session-end leak detector never race-kills an in-flight test on a
#      parallel worker.
GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS: Final[int] = 60 * 60


def gcp_credentials_available() -> bool:
    """Return True iff Google ADC can resolve credentials.

    Used to gate release tests and the session-end cleanup hook (no-op when
    credentials are absent). Delegates to the same
    ``GcpProviderConfig.get_credentials_and_resolved_project`` the provider calls
    at construction time, so the gate and production code agree on what counts as
    "available".
    """
    try:
        GcpProviderConfig().get_credentials_and_resolved_project()
    except (GcpCredentialsError, google_auth_exceptions.GoogleAuthError):
        return False
    return True


def get_default_project() -> str | None:
    """Return the resolved GCP project (or ``MNGR_GCP_PROJECT`` override), if any.

    Routes through the exact resolution the provider uses on the normal create
    path: the ``MNGR_GCP_PROJECT`` env override is mapped onto the config's
    ``project_id`` (so the configured value wins), and resolution otherwise falls
    back to the project ADC resolves -- via ``get_credentials_and_resolved_project``
    + ``resolve_project_id``, the same code production runs. The production path
    raises ``GcpProjectError`` / ``GcpCredentialsError`` when nothing resolves;
    here we translate that to ``None`` so the release tests and the session-end
    scanner can skip cleanly instead of erroring.
    """
    config = GcpProviderConfig(project_id=os.environ.get("MNGR_GCP_PROJECT"))
    try:
        _credentials, adc_project = config.get_credentials_and_resolved_project()
        return config.resolve_project_id(adc_project)
    except (GcpCredentialsError, GcpProjectError, google_auth_exceptions.GoogleAuthError):
        return None


class FakeOperation:
    """Stand-in for a google-cloud-compute ExtendedOperation; ``result()`` no-ops or re-raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def result(self) -> None:
        if self.error is not None:
            raise self.error


class FakeInstancesClient:
    """Fake InstancesClient: records insert/delete/get/list requests, returns canned responses."""

    def __init__(self) -> None:
        self.inserted: list[compute_v1.Instance] = []
        self.deleted: list[str] = []
        self.get_result: compute_v1.Instance | None = None
        self.get_error: Exception | None = None
        self.list_result: list[compute_v1.Instance] = []
        self.last_list_filter: str | None = None
        self.insert_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.list_error: Exception | None = None
        # (zone_scope, instances) pairs returned by ``aggregated_list``.
        self.aggregated_result: list[tuple[str, list[compute_v1.Instance]]] = []
        self.aggregated_list_error: Exception | None = None
        # Instance names passed to ``stop`` / ``start`` (one entry per call).
        self.stopped: list[str] = []
        self.started: list[str] = []
        self.stop_error: Exception | None = None
        self.start_error: Exception | None = None
        # ``set_metadata`` requests (the merged Metadata resource per call).
        self.set_metadata_calls: list[compute_v1.Metadata] = []
        # Errors raised by ``set_metadata``, consumed one per call (head popped),
        # so a ``[PreconditionFailed(...)]`` makes the first call fail and the
        # retry succeed. An entry of ``None`` is a successful call.
        self.set_metadata_errors: list[Exception | None] = []

    def insert(self, *, project: str, zone: str, instance_resource: compute_v1.Instance) -> FakeOperation:
        self.inserted.append(instance_resource)
        return FakeOperation(error=self.insert_error)

    def delete(self, *, project: str, zone: str, instance: str) -> FakeOperation:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(instance)
        return FakeOperation()

    def get(self, *, project: str, zone: str, instance: str) -> compute_v1.Instance:
        if self.get_error is not None:
            raise self.get_error
        assert self.get_result is not None, "get_result not set"
        return self.get_result

    def stop(self, *, project: str, zone: str, instance: str) -> FakeOperation:
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped.append(instance)
        return FakeOperation()

    def start(self, *, project: str, zone: str, instance: str) -> FakeOperation:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(instance)
        return FakeOperation()

    def set_metadata(
        self, *, project: str, zone: str, instance: str, metadata_resource: compute_v1.Metadata
    ) -> FakeOperation:
        # Mirror the real ``setMetadata`` enough for the optimistic-concurrency
        # path: record the merged resource, then (if scripted) raise a preset
        # error for this call so a 412 fingerprint conflict can be exercised.
        self.set_metadata_calls.append(metadata_resource)
        error = self.set_metadata_errors.pop(0) if self.set_metadata_errors else None
        if error is not None:
            raise error
        return FakeOperation()

    def list(self, *, request: compute_v1.ListInstancesRequest) -> list[compute_v1.Instance]:
        # Mirror the real google-cloud-compute API: ``filter`` is carried on the
        # request object, not a flattened kwarg, so a test exercises the same
        # call shape production uses.
        if self.list_error is not None:
            raise self.list_error
        self.last_list_filter = request.filter or None
        return self.list_result

    def aggregated_list(
        self, *, request: compute_v1.AggregatedListInstancesRequest
    ) -> Iterator[tuple[str, compute_v1.InstancesScopedList]]:
        # Mirror the real pager shape: iterating yields ``(zone_scope, scoped_list)``
        # pairs, where ``scoped_list.instances`` is the per-zone instance list.
        # ``aggregated_result`` is keyed by the scope string (e.g. ``zones/us-west1-a``).
        if self.aggregated_list_error is not None:
            raise self.aggregated_list_error
        for scope, scoped_instances in self.aggregated_result:
            yield scope, compute_v1.InstancesScopedList(instances=scoped_instances)


class FakeFirewallsClient:
    """Fake FirewallsClient: ``get`` raises NotFound unless a rule is preset; records inserts."""

    def __init__(self) -> None:
        self.existing: compute_v1.Firewall | None = None
        self.inserted: list[compute_v1.Firewall] = []
        self.insert_error: Exception | None = None
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None

    def get(self, *, project: str, firewall: str) -> compute_v1.Firewall:
        if self.existing is None:
            raise google_api_exceptions.NotFound("firewall not found")
        return self.existing

    def insert(self, *, project: str, firewall_resource: compute_v1.Firewall) -> FakeOperation:
        self.inserted.append(firewall_resource)
        return FakeOperation(error=self.insert_error)

    def delete(self, *, project: str, firewall: str) -> FakeOperation:
        self.deleted.append(firewall)
        return FakeOperation(error=self.delete_error)


class _StubbedGcpVpsClient(GcpVpsClient):
    """Test-only GcpVpsClient that injects fake compute clients.

    Production ``GcpVpsClient`` builds the GCE clients lazily from its resolved
    ADC credentials; this subclass exposes constructor fields that callers can
    populate with hand-written fakes so unit tests exercise the request-building
    and response-handling logic without real API calls. Keeping the test-only
    injection out of the production model means production code never carries a
    field whose sole purpose is test orchestration.
    """

    stubbed_instances_client: Any = Field(default=None, description="Fake InstancesClient")
    stubbed_firewalls_client: Any = Field(default=None, description="Fake FirewallsClient")

    def _instances(self) -> Any:
        return self.stubbed_instances_client

    def _firewalls(self) -> Any:
        return self.stubbed_firewalls_client


# =============================================================================
# In-memory fake GCS + stubbed bucket/volume for tests that need offline host_dir
# without real Google Cloud Storage. Lives here (not in ``state_bucket_test.py``)
# so multiple test modules can import the fakes/stubs uniformly (mirrors
# ``libs/mngr_azure/imbue/mngr_azure/testing.py`` housing ``_StubbedBlobStateBucket``).
# The google-cloud-storage SDK has no first-party in-memory testing harness (no
# moto-equivalent), so the fakes cover exactly the methods production calls.
# =============================================================================

# A credential placeholder for the bucket/volume models: pydantic validates the
# field type, but the fake client never actually authenticates with it.
_FAKE_CREDENTIALS: Final = AnonymousCredentials()


class _FakeBlob:
    """A single object in the fake GCS bucket: name + bytes payload + mtime."""

    def __init__(self, parent: "_FakeBucket", name: str, content: bytes) -> None:
        self.parent = parent
        self.name = name
        self.content = content
        self.updated: datetime = datetime.now(timezone.utc)
        self.size: int = len(content)

    def upload_from_string(self, data: bytes | str) -> None:
        content = data.encode("utf-8") if isinstance(data, str) else data
        self.content = content
        self.size = len(content)
        self.updated = datetime.now(timezone.utc)
        self.parent.blobs[self.name] = self

    def download_as_bytes(self) -> bytes:
        existing = self.parent.blobs.get(self.name)
        if existing is None:
            raise google_api_exceptions.NotFound(f"No such object: {self.name}")
        return existing.content

    def delete(self) -> None:
        if self.name not in self.parent.blobs:
            raise google_api_exceptions.NotFound(f"No such object: {self.name}")
        del self.parent.blobs[self.name]

    def exists(self) -> bool:
        return self.name in self.parent.blobs


class _FakeBucket:
    """In-memory GCS bucket: a name -> blob dict plus metadata."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, _FakeBlob] = {}
        self.storage_class: str = "STANDARD"
        self.labels: dict[str, str] = {}
        # The fake's stand-in for ``Bucket.iam_configuration`` -- only the one
        # attribute production touches.
        self.iam_configuration = _FakeIamConfiguration()
        # Set by ``_FakeStorageClient.create_bucket`` so ``delete()`` can
        # faithfully remove the bucket from its parent registry. None for
        # detached handles that were never registered, in which case
        # ``delete()`` only clears blobs.
        self.parent_client: "_FakeStorageClient | None" = None

    def blob(self, name: str) -> _FakeBlob:
        existing = self.blobs.get(name)
        if existing is not None:
            return existing
        # Mirror the real SDK: ``bucket.blob(name)`` returns a handle whether or
        # not the object exists. The handle's ``exists()`` / ``delete()`` raise
        # NotFound when the underlying object is absent.
        return _FakeBlob(parent=self, name=name, content=b"")

    def delete(self, force: bool = False) -> None:
        del force
        self.blobs.clear()
        # Mirror real GCS: after ``bucket.delete(...)`` the bucket no longer
        # exists, so a subsequent ``lookup_bucket`` returns None. Without this
        # the fake's ``bucket_exists()`` would keep returning True after a
        # production ``delete_bucket()`` and the idempotency contract could
        # only be verified by external test scaffolding (which would be
        # tautological).
        if self.parent_client is not None:
            self.parent_client.buckets.pop(self.name, None)


class _FakeIamConfiguration:
    """Stand-in for ``Bucket.iam_configuration`` -- production only sets one flag."""

    uniform_bucket_level_access_enabled: bool = False


class _FakeListIterator:
    """A list-iterator that also carries a ``prefixes`` attribute (matches the real SDK)."""

    def __init__(self, blobs: list[_FakeBlob], prefixes: set[str]) -> None:
        self._blobs = blobs
        self.prefixes: set[str] = prefixes

    def __iter__(self) -> Iterator[_FakeBlob]:
        return iter(self._blobs)


class _FakeStorageClient:
    """In-memory GCS client: a bucket-name -> _FakeBucket dict + the methods the bucket calls."""

    def __init__(self) -> None:
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        # The real SDK returns a handle without creating the bucket -- the bucket
        # itself only materializes via ``create_bucket``. Mirror that: an existing
        # handle is returned if present, else a fresh detached one.
        existing = self.buckets.get(name)
        if existing is not None:
            return existing
        return _FakeBucket(name)

    def lookup_bucket(self, name: str) -> _FakeBucket | None:
        return self.buckets.get(name)

    def get_bucket(self, name: str) -> _FakeBucket:
        existing = self.buckets.get(name)
        if existing is None:
            raise google_api_exceptions.NotFound(f"No such bucket: {name}")
        return existing

    def create_bucket(self, bucket: _FakeBucket, location: str) -> _FakeBucket:
        del location
        if bucket.name in self.buckets:
            raise google_api_exceptions.Conflict(f"Bucket already exists: {bucket.name}")
        # Bind the bucket to this client so ``bucket.delete(...)`` can remove
        # itself from the registry (mirrors the real GCS lifecycle).
        bucket.parent_client = self
        self.buckets[bucket.name] = bucket
        return bucket

    def list_blobs(
        self,
        bucket_or_name: str | _FakeBucket,
        prefix: str = "",
        delimiter: str | None = None,
        max_results: int | None = None,
    ) -> _FakeListIterator:
        bucket_name = bucket_or_name if isinstance(bucket_or_name, str) else bucket_or_name.name
        bucket = self.buckets.get(bucket_name)
        if bucket is None:
            raise google_api_exceptions.NotFound(f"No such bucket: {bucket_name}")
        files: list[_FakeBlob] = []
        prefixes: set[str] = set()
        for blob in bucket.blobs.values():
            if not blob.name.startswith(prefix):
                continue
            if delimiter is None:
                files.append(blob)
                continue
            # Delimited: split the trailing part on the delimiter; if a delimiter
            # appears, classify the immediate-child portion as a sub-"directory".
            tail = blob.name[len(prefix) :]
            if delimiter in tail:
                sub = prefix + tail.split(delimiter, 1)[0] + delimiter
                prefixes.add(sub)
            else:
                files.append(blob)
            if max_results is not None and len(files) >= max_results:
                break
        if max_results is not None:
            files = files[:max_results]
        return _FakeListIterator(blobs=files, prefixes=prefixes)


class _StubbedGcsVolume(GcsVolume):
    """Test-only ``GcsVolume`` that injects a fake storage client via a constructor field.

    Mirrors ``_StubbedGcpVpsClient`` above: production ``GcsVolume._client()``
    builds a real ``storage.Client`` lazily; this subclass routes it to the
    injected fake instead, so the test exercises the request-building and
    response-handling without real GCS calls and without monkeypatching the
    module.
    """

    stubbed_storage_client: Any = Field(default=None, description="Fake storage client")

    def _client(self) -> Any:
        return self.stubbed_storage_client


class _StubbedGcsStateBucket(GcsStateBucket):
    """Test-only ``GcsStateBucket`` that injects a fake storage client + matching volume.

    Overrides ``_make_host_dir_volume`` to produce a ``_StubbedGcsVolume`` bound
    to the same fake, so seeded objects on the bucket are visible to the volume
    reads (the production volume builds its own fresh client otherwise).
    """

    stubbed_storage_client: Any = Field(default=None, description="Fake storage client")

    def _client(self) -> Any:
        return self.stubbed_storage_client

    def _make_host_dir_volume(self) -> Volume:
        return _StubbedGcsVolume(
            credentials=self.credentials,
            project_id=self.project_id,
            bucket_name=self.bucket_name,
            stubbed_storage_client=self.stubbed_storage_client,
        )
