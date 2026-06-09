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
from google.auth import exceptions as google_auth_exceptions
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
    when credentials are absent). Matches what ``GcpProviderConfig.get_credentials``
    does at provider-construction time, so the gate and production code agree on
    what counts as "available".
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
