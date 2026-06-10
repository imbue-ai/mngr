"""Shared test helpers and constants for mngr_gcp.

Lives outside ``conftest.py`` so other test modules (e.g. ``test_release_gcp``)
can import these directly; importing from a ``conftest.py`` is a pytest
anti-pattern (those files are auto-discovered, not designed for direct import).
Mirrors ``libs/mngr_aws/imbue/mngr_aws/testing.py``.
"""

import os
from typing import Any
from typing import Final

import google.auth
from google.api_core import exceptions as google_api_exceptions
from google.auth import exceptions as google_auth_exceptions
from google.cloud import compute_v1
from pydantic import Field

from imbue.mngr_gcp.client import GcpVpsClient

# Optional prefix release tests use for their agent names so leaked instances
# (should the scanner ever fail) are still visually identifiable as test-owned.
# Cleanup logic does NOT depend on this -- ``GcpVpsClient.create_instance``
# labels pytest-launched instances with ``mngr-pytest-launched=true`` and the
# conftest scanner filters on that label.
GCP_TEST_NAME_PREFIX: Final[str] = "test-gcp-"

# Zone used by the GCP release tests and the session-end leak scan. Tests can
# override via ``MNGR_GCP_ZONE``; defaults to ``us-west1-a`` to match the
# verified project. Read once at import time so conftest and test_release_gcp
# observe the same value.
GCP_DEFAULT_ZONE: Final[str] = os.environ.get("MNGR_GCP_ZONE", "us-west1-a")

# Region derived from the zone for the firewall / settings.toml.
GCP_DEFAULT_REGION: Final[str] = GCP_DEFAULT_ZONE.rsplit("-", 1)[0]

# Release-test opt-in flag. Mirrors the gate that ``test_release_gcp.py`` uses
# on ``pytestmark`` and that ``conftest.py`` uses to suppress the session-end
# orphan scan when no release tests were requested.
GCP_RELEASE_TESTS_OPT_IN: Final[bool] = os.environ.get("MNGR_GCP_RELEASE_TESTS") == "1"

# Single source of truth for the release-test instance lifetime. Used in two
# places that must stay aligned:
#   1. ``test_release_gcp.py`` writes it into a tmp-path settings.toml
#      (``[providers.gcp] auto_shutdown_minutes``) so each instance launches
#      with scheduling.max_run_duration + instance_termination_action=DELETE.
#   2. ``conftest.py`` derives the orphan-scan grace period from this value so
#      the session-end leak detector never race-kills an in-flight test on a
#      parallel worker.
GCP_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES: Final[int] = 60


def gcp_credentials_available() -> bool:
    """Return True iff Google ADC can resolve credentials.

    Used to gate release tests (skipif) and the session-end cleanup hook (no-op
    when credentials are absent). Matches what
    ``GcpProviderConfig.get_credentials_and_resolved_project`` does at
    provider-construction time, so the gate and production code agree on what
    counts as "available".
    """
    try:
        credentials, _project = google.auth.default()
    except google_auth_exceptions.GoogleAuthError:
        return False
    return credentials is not None


def get_default_project() -> str | None:
    """Return the ADC-resolved project (or ``MNGR_GCP_PROJECT`` override), if any.

    The release tests and the session-end scanner need a project ID. Prefer an
    explicit ``MNGR_GCP_PROJECT`` env override; otherwise fall back to the
    project ADC resolves. Returns None when neither is available so callers can
    skip cleanly.
    """
    env_project = os.environ.get("MNGR_GCP_PROJECT")
    if env_project:
        return env_project
    try:
        _credentials, project = google.auth.default()
    except google_auth_exceptions.GoogleAuthError:
        return None
    return project


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

    def list(self, *, request: compute_v1.ListInstancesRequest) -> list[compute_v1.Instance]:
        # Mirror the real google-cloud-compute API: ``filter`` is carried on the
        # request object, not a flattened kwarg, so a test exercises the same
        # call shape production uses.
        if self.list_error is not None:
            raise self.list_error
        self.last_list_filter = request.filter or None
        return self.list_result


class FakeFirewallsClient:
    """Fake FirewallsClient: ``get`` raises NotFound unless a rule is preset; records inserts."""

    def __init__(self) -> None:
        self.existing: compute_v1.Firewall | None = None
        self.inserted: list[compute_v1.Firewall] = []
        self.insert_error: Exception | None = None

    def get(self, *, project: str, firewall: str) -> compute_v1.Firewall:
        if self.existing is None:
            raise google_api_exceptions.NotFound("firewall not found")
        return self.existing

    def insert(self, *, project: str, firewall_resource: compute_v1.Firewall) -> FakeOperation:
        self.inserted.append(firewall_resource)
        return FakeOperation(error=self.insert_error)


class FakeSnapshotsClient:
    """Fake SnapshotsClient: records snapshot insert/delete/list, returns canned responses."""

    def __init__(self) -> None:
        self.inserted: list[compute_v1.Snapshot] = []
        self.deleted: list[str] = []
        self.list_result: list[compute_v1.Snapshot] = []

    def insert(self, *, project: str, snapshot_resource: compute_v1.Snapshot) -> FakeOperation:
        self.inserted.append(snapshot_resource)
        return FakeOperation()

    def delete(self, *, project: str, snapshot: str) -> FakeOperation:
        self.deleted.append(snapshot)
        return FakeOperation()

    def list(self, *, project: str) -> list[compute_v1.Snapshot]:
        return self.list_result


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
    stubbed_snapshots_client: Any = Field(default=None, description="Fake SnapshotsClient")

    def _instances(self) -> Any:
        return self.stubbed_instances_client

    def _firewalls(self) -> Any:
        return self.stubbed_firewalls_client

    def _snapshots(self) -> Any:
        return self.stubbed_snapshots_client
