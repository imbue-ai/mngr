"""End-to-end release tests for the GCP provider.

These tests provision and destroy real GCE instances. They cost real money --
typically a few cents per run for a ~5-minute e2-small -- and are double-gated:

- Google ADC must be resolvable (``google.auth.default()``). See
  ``testing.gcp_credentials_available`` -- the same probe used by the
  session-end cleanup hook.
- ``MNGR_GCP_RELEASE_TESTS=1`` must be set explicitly.

Three layers of damage control prevent leaked GCE cost (see ``conftest.py`` in
this package for the full picture):

1. Each test's ``finally`` calls ``mngr destroy --force``.
2. ``pytest_sessionfinish`` in ``conftest.py`` force-deletes any instance
   labeled ``mngr-pytest-launched=true`` (added by
   ``GcpVpsClient.create_instance`` whenever ``PYTEST_CURRENT_TEST`` is set) and
   older than the TTL at session end, and fails the session.
3. The subprocess that runs ``mngr create`` is pointed at a temporary
   ``settings.toml`` (via ``MNGR_PROJECT_CONFIG_DIR``) that sets
   ``[providers.gcp] auto_shutdown_seconds``. This launches each instance with
   ``scheduling.max_run_duration`` + ``instance_termination_action=DELETE``, so
   the VM self-deletes even if pytest itself is killed. The production
   GcpProvider refuses to create instances under pytest without this set.

Run manually (release tests do not run in CI):

    MNGR_GCP_RELEASE_TESTS=1 PYTEST_MAX_DURATION_SECONDS=1800 \\
        uv run pytest --no-cov -n 0 -m release \\
        libs/mngr_gcp/imbue/mngr_gcp/test_release_gcp.py

The ``PYTEST_MAX_DURATION_SECONDS=1800`` is important: the provider release
trips each boot a real GCE VM serially (``-n 0``), which exceeds the default
~600s budget. That env var sets the pytest global-lock deadline -- once it passes, a
concurrent pytest run reclaims (kills) this one -- so a too-low value SIGTERMs
the suite mid-test and can leak a VM. Invoke ``uv run pytest`` directly (not
``just test``, whose recipe hardcodes the 600s budget). Other long real-resource
release tests follow the same convention (e.g. ``test_adopt_session`` at 1500).
"""

import os
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import google.auth
import pytest

from imbue.mngr.providers.provider_release_testing import run_provider_release_trip1
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip2
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip3
from imbue.mngr.providers.provider_release_testing import run_provider_release_trip4
from imbue.mngr_gcp.client import GCP_PYTEST_LAUNCHED_LABEL
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.testing import GCP_DEFAULT_REGION
from imbue.mngr_gcp.testing import GCP_DEFAULT_ZONE
from imbue.mngr_gcp.testing import GCP_RELEASE_TESTS_OPT_IN
from imbue.mngr_gcp.testing import GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_gcp.testing import GCP_TEST_NAME_PREFIX
from imbue.mngr_gcp.testing import gcp_credentials_available
from imbue.mngr_gcp.testing import get_default_project
from imbue.mngr_vps.primitives import IsolationMode
from imbue.mngr_vps.testing import VpsCloudReleaseProfile
from imbue.mngr_vps.testing import find_handle_by_launched_label

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    # Skip only when the user did not opt in. Opting in but lacking credentials is
    # a misconfiguration, handled by ``_fail_if_opted_in_without_credentials``
    # below: it fails loudly rather than skipping, so a release-test run that the
    # user explicitly requested but that cannot reach GCP is visible instead of
    # silently reported as "skipped".
    pytest.mark.skipif(
        not GCP_RELEASE_TESTS_OPT_IN,
        reason="MNGR_GCP_RELEASE_TESTS=1 not set",
    ),
]


@pytest.fixture(autouse=True)
def _fail_if_opted_in_without_credentials() -> None:
    """Fail (not skip) when release tests were opted into but ADC is unresolvable.

    The ``skipif`` above has already excluded the not-opted-in case, so reaching
    here means ``MNGR_GCP_RELEASE_TESTS=1`` is set. If credentials cannot be
    resolved the run is misconfigured -- fail explicitly rather than let the test
    pass as skipped, which would hide that the requested run never executed.
    """
    if not gcp_credentials_available():
        pytest.fail(
            "MNGR_GCP_RELEASE_TESTS=1 is set but GCP Application Default Credentials could not be "
            "resolved, so the release tests cannot run. Run 'gcloud auth application-default login' "
            "(or set GOOGLE_APPLICATION_CREDENTIALS), or unset MNGR_GCP_RELEASE_TESTS to skip them."
        )


@pytest.fixture(scope="session")
def gcp_release_test_project() -> str:
    """Resolve the GCP project used by the release tests (ADC project or env override)."""
    project = get_default_project()
    assert project is not None, "no GCP project resolved (set MNGR_GCP_PROJECT or configure ADC)"
    return project


def _write_release_settings(settings_dir: Path, project: str, *, isolation: str | None = None) -> None:
    """Write the release-test ``settings.toml`` into ``settings_dir``.

    Shared by the prepare fixture and the per-test settings fixture so both the
    ``mngr gcp prepare`` and ``mngr create`` subprocesses load the same opted-in
    config. ``is_allowed_in_pytest = true`` is required because the subprocesses
    inherit ``PYTEST_CURRENT_TEST`` and mngr refuses to load any config that does
    not opt in -- without it, a developer machine with a real mngr profile would
    fail before any GCP call.

    ``isolation`` selects the placement shape: ``None`` leaves the default
    (Docker container); ``"NONE"`` writes ``isolation = "NONE"`` so the bare
    (no-container) realizer runs the agent directly on the GCE instance's OS.
    """
    isolation_line = f'isolation = "{isolation}"\n' if isolation is not None else ""
    (settings_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        "\n[providers.gcp]\n"
        'backend = "gcp"\n'
        f"{isolation_line}"
        f'project_id = "{project}"\n'
        f'default_region = "{GCP_DEFAULT_REGION}"\n'
        f'default_zone = "{GCP_DEFAULT_ZONE}"\n'
        # Self-delete via max_run_duration if pytest is killed before the
        # per-test cleanup runs.
        f"auto_shutdown_seconds = {GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        # Open the firewall to the public internet so the test SSH connection
        # (from the developer laptop / CI runner) works without caller-IP
        # discovery. Production callers must pick a tight CIDR; the instance only
        # lives for the duration of the test and is then destroyed.
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n'
        # Disable other remote providers so the create-host preflight doesn't
        # trip on them looking for credentials.
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.azure]\nis_enabled = false\n"
        "\n[providers.aws]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )


@pytest.fixture(scope="session")
def _gcp_release_test_firewall_prepared(
    gcp_release_test_project: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Run ``mngr gcp prepare`` once per test session before any lifecycle test.

    ``create_instance`` only resolves the firewall read-only on the hot path (so
    users with restricted IAM can run mngr create); the privileged
    firewall-creation step lives in ``mngr gcp prepare``. The release tests need
    to run prepare once so subsequent creates can resolve the rule. ``0.0.0.0/0``
    is used so the test SSH connection works without caller-IP discovery; the
    instance only lives for the test and is then destroyed.

    Runs against an opted-in test ``settings.toml`` (via ``MNGR_PROJECT_CONFIG_DIR``)
    and an isolated mngr home (``MNGR_HOST_DIR`` + ``HOME``) so the subprocess
    doesn't load the developer's real mngr *profile*
    (``$MNGR_HOST_DIR/profiles/.../settings.toml``), which the pytest guard
    rejects. This session-scoped fixture runs before the per-test host-dir
    isolation, so it must isolate the host dir itself; ``CLOUDSDK_CONFIG`` is
    pinned to the real gcloud config so ADC still resolves under the swapped HOME
    (mirrors what ``conftest.setup_test_mngr_env`` does for the per-test
    subprocesses).
    """
    settings_dir = tmp_path_factory.mktemp("gcp_prepare_settings")
    _write_release_settings(settings_dir, gcp_release_test_project)
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    env["MNGR_HOST_DIR"] = str(tmp_path_factory.mktemp("gcp_prepare_mngr_home"))
    env["HOME"] = str(tmp_path_factory.mktemp("gcp_prepare_home"))
    if "GOOGLE_APPLICATION_CREDENTIALS" not in env:
        env["CLOUDSDK_CONFIG"] = env.get("CLOUDSDK_CONFIG") or str(Path.home() / ".config" / "gcloud")
    cmd = [
        "uv",
        "run",
        "mngr",
        "gcp",
        "prepare",
        "--project",
        gcp_release_test_project,
        "--zone",
        GCP_DEFAULT_ZONE,
        "--allowed-ssh-cidr",
        "0.0.0.0/0",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, (
        f"`mngr gcp prepare` failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


# =============================================================================
# Trip 1 -- the shared provider release lifecycle (create -> stop/start ->
# sketchy kill -> gc), parametrized over isolation mode. See
# `imbue.mngr.providers.provider_release_testing` and
# `specs/provider-release-tests.md`.
# =============================================================================


class _GcpReleaseProfile(VpsCloudReleaseProfile):
    """GCP plumbing for the shared provider release trip."""

    provider_name = "gcp"
    name_prefix = GCP_TEST_NAME_PREFIX

    # Trip 4: GCP curates the missing-credential help text toward the ADC login command (the
    # spec's divergence was fixed in this PR -- see `_gcp_not_authorized_error` in mngr_gcp/backend.py).
    has_curated_unavailable_help = True
    credential_setup_command = "gcloud auth application-default login"
    # GCP captures host_dir to the GCS state bucket at `mngr stop`, so a stopped host's host_dir is
    # readable offline (Trip 1's opt-in offline-host_dir step). Matches AWS / Azure.
    supports_offline_host_dir = True

    def __init__(self, client: GcpVpsClient, isolation: IsolationMode, project: str) -> None:
        super().__init__(client, isolation)
        self._gcp_client = client
        self._project = project

    def unavailable_reason(self) -> str | None:
        if not (gcp_credentials_available() and GCP_RELEASE_TESTS_OPT_IN):
            return "GCP ADC or MNGR_GCP_RELEASE_TESTS=1 not set"
        return None

    def write_settings(self, settings_dir: Path) -> None:
        _write_release_settings(
            settings_dir, self._project, isolation="NONE" if self._isolation is IsolationMode.NONE else None
        )

    def create_extra_args(self) -> Sequence[str]:
        return ()

    def make_credentials_unresolvable_env(self) -> Mapping[str, str | None]:
        # Point ADC's well-known-file resolution at an empty location and clear every project /
        # key env var so ``google.auth.default()`` finds nothing and raises
        # ``DefaultCredentialsError`` (a ``GoogleAuthError``) -> the contract
        # ``ProviderUnavailableError``. The conftest pins ``CLOUDSDK_CONFIG`` to the real gcloud
        # dir, so overriding it to a nonexistent path is what actually hides ADC.
        return {
            "CLOUDSDK_CONFIG": "/nonexistent/gcloud/config",
            "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/gcloud/adc.json",
            "GOOGLE_CLOUD_PROJECT": None,
            "GCLOUD_PROJECT": None,
            "MNGR_GCP_PROJECT": None,
        }

    def find_launched_host_handle(self, host_name: str) -> str | None:
        return find_handle_by_launched_label(self._gcp_client.list_instances(), GCP_PYTEST_LAUNCHED_LABEL)


@pytest.mark.rsync
@pytest.mark.parametrize("isolation", [IsolationMode.CONTAINER, IsolationMode.NONE])
def test_provider_release_trip1(
    isolation: IsolationMode,
    tmp_path: Path,
    temp_git_repo: Path,
    gcp_release_client: GcpVpsClient,
    gcp_release_test_project: str,
    _gcp_release_test_firewall_prepared: None,
) -> None:
    run_provider_release_trip1(
        _GcpReleaseProfile(gcp_release_client, isolation, gcp_release_test_project), tmp_path, temp_git_repo
    )


@pytest.mark.rsync
@pytest.mark.parametrize("isolation", [IsolationMode.CONTAINER, IsolationMode.NONE])
def test_provider_release_trip2(
    isolation: IsolationMode,
    tmp_path: Path,
    temp_git_repo: Path,
    gcp_release_client: GcpVpsClient,
    gcp_release_test_project: str,
    _gcp_release_test_firewall_prepared: None,
) -> None:
    run_provider_release_trip2(
        _GcpReleaseProfile(gcp_release_client, isolation, gcp_release_test_project), tmp_path, temp_git_repo
    )


@pytest.mark.rsync
@pytest.mark.parametrize("isolation", [IsolationMode.CONTAINER, IsolationMode.NONE])
def test_provider_release_trip3(
    isolation: IsolationMode,
    tmp_path: Path,
    temp_git_repo: Path,
    gcp_release_client: GcpVpsClient,
    gcp_release_test_project: str,
    _gcp_release_test_firewall_prepared: None,
) -> None:
    run_provider_release_trip3(
        _GcpReleaseProfile(gcp_release_client, isolation, gcp_release_test_project), tmp_path, temp_git_repo
    )


def test_provider_release_trip4(
    tmp_path: Path,
    temp_git_repo: Path,
    gcp_release_client: GcpVpsClient,
    gcp_release_test_project: str,
) -> None:
    # No-boot CLI error-classification trip: not parametrized over isolation (the error paths are
    # isolation-agnostic) and no ``rsync`` mark (it never provisions a host). No firewall-prepare
    # dependency either -- nothing is created.
    run_provider_release_trip4(
        _GcpReleaseProfile(gcp_release_client, IsolationMode.CONTAINER, gcp_release_test_project),
        tmp_path,
        temp_git_repo,
    )


# =============================================================================
# API client smoke tests (real network calls, read-only)
# =============================================================================


@pytest.fixture()
def gcp_release_client(gcp_release_test_project: str) -> GcpVpsClient:
    """Real GCP API client for release-test read-only calls."""
    credentials, _project = google.auth.default()
    return GcpVpsClient(
        credentials=credentials,
        project_id=gcp_release_test_project,
        zone=GCP_DEFAULT_ZONE,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts",
    )


def test_api_client_list_instances_does_not_error(gcp_release_client: GcpVpsClient) -> None:
    instances = gcp_release_client.list_instances()
    assert isinstance(instances, list)
