"""Pytest fixtures and session-finish leak detection for mngr_gcp tests.

Modeled after ``libs/mngr_aws/imbue/mngr_aws/conftest.py``. Provides the same
three-layer safety net for GCP release tests so killed test runs cannot leak
GCE cost:

1. Per-test cleanup happens via ``mngr destroy --force`` in each test's
   ``finally`` block (in ``test_release_gcp.py``).
2. ``pytest_sessionfinish`` here scans for leaked GCE instances labeled
   ``mngr-pytest-launched=true`` at the end of the session, force-deletes any
   matches older than the TTL, and fails the session.
3. Each test instance is launched with ``scheduling.max_run_duration`` +
   ``instance_termination_action=DELETE``, which self-deletes the instance even
   if pytest itself is killed.

Layer 2 relies solely on a label scan because the release-test path spawns
``mngr create`` in a subprocess, so there is no Python hand-off back to the test
process where an in-process tracking list would live.

Also registers the shared plugin-test fixtures (including ``temp_mngr_ctx``) so
backend-level unit tests can construct real provider instances.
"""

import os
from collections.abc import Generator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest
from google.api_core import exceptions as google_api_exceptions
from google.cloud import compute_v1
from loguru import logger

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import setup_mngr_test_environment
from imbue.mngr_gcp.cleanup import find_old_test_instances
from imbue.mngr_gcp.cleanup import force_delete_instances
from imbue.mngr_gcp.client import GCP_PYTEST_LAUNCHED_LABEL
from imbue.mngr_gcp.testing import GCP_DEFAULT_ZONE
from imbue.mngr_gcp.testing import GCP_RELEASE_TESTS_OPT_IN
from imbue.mngr_gcp.testing import GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_gcp.testing import gcp_credentials_available
from imbue.mngr_gcp.testing import get_default_project

register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def setup_test_mngr_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Override mngr's autouse env setup to pin the gcloud ADC location before HOME swap.

    HOME isolation hides ``~/.config/gcloud/application_default_credentials.json``
    from the test process, which makes ``google.auth.default()`` fail to resolve
    the well-known ADC file even when the real shell has working creds. Pin
    ``CLOUDSDK_CONFIG`` to the real gcloud config directory (computed before HOME
    is swapped) so ADC resolution survives isolation. An explicit
    ``GOOGLE_APPLICATION_CREDENTIALS`` (service-account key) is an absolute path
    that already survives the HOME swap, so no extra handling is needed there.
    """
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        # Mirror google.auth's well-known-file resolution: CLOUDSDK_CONFIG if
        # set, else ~/.config/gcloud (mac/linux). Compute it against the real
        # HOME before setup_mngr_test_environment swaps HOME.
        cloud_sdk_config = os.environ.get("CLOUDSDK_CONFIG") or str(Path.home() / ".config" / "gcloud")
        monkeypatch.setenv("CLOUDSDK_CONFIG", cloud_sdk_config)
    setup_mngr_test_environment(tmp_home_dir, temp_host_dir, mngr_test_prefix, mngr_test_root_name, monkeypatch)
    yield


# Orphan-scan grace period. An instance younger than this is left alone to avoid
# race-killing an in-flight test on a parallel worker. Derived from the shared
# auto-shutdown TTL (the same value release tests propagate into the instance's
# max_run_duration) so the two TTLs can never drift.
_TEST_LEAK_TTL: Final[timedelta] = timedelta(seconds=GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS)


def _mark_session_failed(session: pytest.Session) -> None:
    """Fail the session, but only if it was otherwise passing.

    Raising from ``pytest_sessionfinish`` is silently dropped by pytest, so
    setting ``session.exitstatus`` is the supported way to signal failure.
    Only overwrite a successful (0) status: a non-zero status
    (INTERRUPTED=2, INTERNAL_ERROR=3, USAGE_ERROR=4, NO_TESTS_COLLECTED=5)
    carries strictly more diagnostic information than TESTS_FAILED=1, so
    downgrading would hide the real reason CI failed.
    """
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Detect and clean up leaked GCP resources at session end.

    Implemented as a pytest hook (not a fixture) so it runs after every
    session-scoped fixture teardown. No-ops when release tests were not opted
    into (``MNGR_GCP_RELEASE_TESTS`` is unset) -- an ordinary run that never
    touches GCE. When the opt-in *is* set but ADC cannot be resolved (or no
    default project is configured, or the client cannot be built), the session
    is *failed* rather than skipped: a release run that cannot authenticate is
    a misconfiguration, not a benign skip, and skipping would silently green a
    run that could not have scanned for leaks. If leaks are found they are
    force-deleted and the session fails. All failure paths set
    ``session.exitstatus`` only when the session was otherwise passing, so a
    more-specific failure is preserved.
    """
    del exitstatus
    if not GCP_RELEASE_TESTS_OPT_IN:
        return
    if not gcp_credentials_available():
        logger.error(
            "MNGR_GCP_RELEASE_TESTS=1 is set but GCP Application Default Credentials could not be "
            "resolved, so the session-end leak scan cannot run. Configure ADC, or unset "
            "MNGR_GCP_RELEASE_TESTS to skip the GCP release tests."
        )
        _mark_session_failed(session)
        return
    project = get_default_project()
    if project is None:
        logger.error(
            "MNGR_GCP_RELEASE_TESTS=1 is set but no default GCP project could be resolved, so the "
            "session-end leak scan cannot run. Configure a project, or unset MNGR_GCP_RELEASE_TESTS."
        )
        _mark_session_failed(session)
        return

    try:
        instances_client = compute_v1.InstancesClient()
    except google_api_exceptions.GoogleAPICallError as e:
        logger.error("Failed to build InstancesClient for session-end leak scan: {}", e)
        _mark_session_failed(session)
        return

    orphans = find_old_test_instances(
        instances_client, project, GCP_DEFAULT_ZONE, _TEST_LEAK_TTL, datetime.now(timezone.utc)
    )
    if not orphans:
        return

    force_delete_instances(instances_client, project, GCP_DEFAULT_ZONE, orphans)
    message = (
        "=" * 70
        + "\nGCP SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
        + "=" * 70
        + f"\n\nLeaked GCE instances labeled {GCP_PYTEST_LAUNCHED_LABEL}=true and "
        + f"older than {GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS // 60} minutes:\n  "
        + "\n  ".join(orphans)
        + "\n\nInstances have been force-deleted, but tests should not leak.\n"
    )
    logger.error(message)
    _mark_session_failed(session)
