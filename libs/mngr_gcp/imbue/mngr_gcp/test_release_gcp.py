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
   ``[providers.gcp] auto_shutdown_minutes``. This launches each instance with
   ``scheduling.max_run_duration`` + ``instance_termination_action=DELETE``, so
   the VM self-deletes even if pytest itself is killed. The production
   GcpProvider refuses to create instances under pytest without this set.

Run manually (release tests do not run in CI):

    MNGR_GCP_RELEASE_TESTS=1 PYTEST_MAX_DURATION_SECONDS=1800 \\
        uv run pytest --no-cov -n 0 -m release \\
        libs/mngr_gcp/imbue/mngr_gcp/test_release_gcp.py

The ``PYTEST_MAX_DURATION_SECONDS=1800`` is important: the two lifecycle tests
each boot a real GCE VM serially (``-n 0``), which exceeds the default ~600s
budget. That env var sets the pytest global-lock deadline -- once it passes, a
concurrent pytest run reclaims (kills) this one -- so a too-low value SIGTERMs
the suite mid-test and can leak a VM. Invoke ``uv run pytest`` directly (not
``just test``, whose recipe hardcodes the 600s budget). Other long real-resource
release tests follow the same convention (e.g. ``test_adopt_session`` at 1500).
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import google.auth
import pytest

from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.testing import GCP_DEFAULT_REGION
from imbue.mngr_gcp.testing import GCP_DEFAULT_ZONE
from imbue.mngr_gcp.testing import GCP_RELEASE_TESTS_OPT_IN
from imbue.mngr_gcp.testing import GCP_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES
from imbue.mngr_gcp.testing import GCP_TEST_NAME_PREFIX
from imbue.mngr_gcp.testing import gcp_credentials_available
from imbue.mngr_gcp.testing import get_default_project

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (gcp_credentials_available() and GCP_RELEASE_TESTS_OPT_IN),
        reason="GCP ADC or MNGR_GCP_RELEASE_TESTS=1 not set",
    ),
]


@pytest.fixture(scope="session")
def gcp_release_test_project() -> str:
    """Resolve the GCP project used by the release tests (ADC project or env override)."""
    project = get_default_project()
    assert project is not None, "no GCP project resolved (set MNGR_GCP_PROJECT or configure ADC)"
    return project


def _write_release_settings(settings_dir: Path, project: str) -> None:
    """Write the release-test ``settings.toml`` into ``settings_dir``.

    Shared by the prepare fixture and the per-test settings fixture so both the
    ``mngr gcp prepare`` and ``mngr create`` subprocesses load the same opted-in
    config. ``is_allowed_in_pytest = true`` is required because the subprocesses
    inherit ``PYTEST_CURRENT_TEST`` and mngr refuses to load any config that does
    not opt in -- without it, a developer machine with a real mngr profile would
    fail before any GCP call.
    """
    (settings_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        "\n[providers.gcp]\n"
        'backend = "gcp"\n'
        f'project_id = "{project}"\n'
        f'default_region = "{GCP_DEFAULT_REGION}"\n'
        f'default_zone = "{GCP_DEFAULT_ZONE}"\n'
        # Self-delete via max_run_duration if pytest is killed before the
        # per-test cleanup runs.
        f"auto_shutdown_minutes = {GCP_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES}\n"
        # Open the firewall to the public internet so the test SSH connection
        # (from the developer laptop / CI runner) works without caller-IP
        # discovery. Production callers must pick a tight CIDR; the instance only
        # lives for the duration of the test and is then destroyed.
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n'
        # Disable other remote providers so the create-host preflight doesn't
        # trip on them looking for credentials.
        "\n[providers.modal]\nis_enabled = false\n"
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

    ``create_instance`` no longer auto-creates the firewall on the hot path (so
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


@pytest.fixture()
def gcp_test_settings_dir(
    tmp_path: Path,
    gcp_release_test_project: str,
    _gcp_release_test_firewall_prepared: None,
) -> Iterator[Path]:
    """Write a project settings.toml that sets the GCP project + auto-shutdown TTL.

    The release tests must set ``auto_shutdown_minutes`` on the GCP provider
    config so the GCE-native self-delete safety net actually fires; the
    production GcpProvider refuses to create an instance under pytest without it.
    Using ``MNGR_PROJECT_CONFIG_DIR`` to point the subprocess at this settings
    file keeps the test-only TTL out of production code paths.
    """
    _write_release_settings(tmp_path, gcp_release_test_project)
    yield tmp_path


def _run_mngr(
    project_config_dir: Path,
    cwd: Path,
    *args: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a mngr command with the test settings.toml in scope.

    ``cwd`` must be inside a git repository -- ``mngr create`` reads the source
    from the current git checkout unless ``--from`` is passed. The release tests
    supply the ``temp_git_repo`` fixture for this.

    Streams stdout+stderr to a file under ``project_config_dir`` rather than
    buffering with ``capture_output=True``. The buffered mode loses everything on
    ``TimeoutExpired``, which makes diagnosing a stuck ``mngr create``
    impossible.
    """
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    cmd = ["uv", "run", "mngr", *args]
    log_path = Path(project_config_dir) / f"mngr-{args[0] if args else 'cmd'}.log"
    with log_path.open("w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            # 124 is the GNU-coreutils ``timeout`` convention.
            returncode = 124
    log_text = log_path.read_text()
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=log_text,
        stderr=""
        if returncode == 0
        else (
            "see stdout (subprocess stderr was merged into stdout)\n"
            + (f"subprocess timed out after {timeout}s\n" if returncode == 124 else "")
        ),
    )


# =============================================================================
# Provider lifecycle (full create / exec / stop / start / destroy)
# =============================================================================


@pytest.mark.rsync
def test_provider_lifecycle_create_exec_and_destroy(
    gcp_test_settings_dir: Path,
    temp_git_repo: Path,
) -> None:
    agent_name = f"{GCP_TEST_NAME_PREFIX}{int(time.time()) % 100000}"

    result = _run_mngr(
        gcp_test_settings_dir,
        temp_git_repo,
        "create",
        agent_name,
        "--type",
        "command",
        "--provider",
        "gcp",
        "--no-connect",
        "--",
        "sleep",
        "99999",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}\n--- stdout ---\n{result.stdout}"
    assert "successfully" in result.stdout.lower(), f"unexpected create output: {result.stdout}"

    try:
        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "exec", agent_name, "echo hello-from-gcp")
        assert result.returncode == 0, f"Exec failed: {result.stderr}"
        assert "hello-from-gcp" in result.stdout

        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "exec", agent_name, "test -d /mngr && echo exists")
        assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
        assert "exists" in result.stdout

        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "list")
        assert result.returncode == 0, f"List failed: {result.stderr}"
        assert agent_name in result.stdout
        assert "gcp" in result.stdout
    finally:
        # --force skips the destroy confirmation. Result intentionally not
        # checked: best-effort cleanup.
        _run_mngr(gcp_test_settings_dir, temp_git_repo, "destroy", agent_name, "--force", timeout=180)
        time.sleep(20)


@pytest.mark.rsync
def test_provider_lifecycle_create_stop_start_destroy(
    gcp_test_settings_dir: Path,
    temp_git_repo: Path,
) -> None:
    agent_name = f"{GCP_TEST_NAME_PREFIX}ss-{int(time.time()) % 100000}"

    result = _run_mngr(
        gcp_test_settings_dir,
        temp_git_repo,
        "create",
        agent_name,
        "--type",
        "command",
        "--provider",
        "gcp",
        "--no-connect",
        "--",
        "sleep",
        "99999",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}\n--- stdout ---\n{result.stdout}"
    assert "successfully" in result.stdout.lower(), f"unexpected create output: {result.stdout}"

    try:
        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "stop", agent_name)
        assert result.returncode == 0, f"Stop failed: {result.stderr}"

        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "list")
        assert result.returncode == 0
        assert agent_name in result.stdout

        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "start", agent_name, "--no-connect")
        assert result.returncode == 0, f"Start failed: {result.stderr}"

        result = _run_mngr(gcp_test_settings_dir, temp_git_repo, "exec", agent_name, "echo alive-after-restart")
        assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
        assert "alive-after-restart" in result.stdout
    finally:
        _run_mngr(gcp_test_settings_dir, temp_git_repo, "destroy", agent_name, "--force", timeout=180)
        time.sleep(20)


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


def test_api_client_list_snapshots_does_not_error(gcp_release_client: GcpVpsClient) -> None:
    snapshots = gcp_release_client.list_snapshots()
    assert isinstance(snapshots, list)
