"""End-to-end release tests for the Azure provider.

These tests provision and destroy real VMs on Azure. They cost real money --
typically a few cents per run for a ~5-minute Standard_B2s -- and are gated:

- ``MNGR_AZURE_RELEASE_TESTS=1`` must be set explicitly. When it is unset the
  whole module is skipped.
- Once opted in, Azure credentials must be resolvable via
  ``DefaultAzureCredential`` (an ``az login`` session, a service principal, or a
  managed identity; see ``testing.azure_credentials_available``, the same probe
  the session-end cleanup hook uses) and a subscription must be resolvable
  (``AZURE_SUBSCRIPTION_ID`` / ``MNGR_AZURE_SUBSCRIPTION_ID`` / the active ``az``
  subscription). Opting in without either makes the tests *fail* (not skip), so
  a run the user explicitly requested but that cannot reach Azure is visible
  rather than silently reported as "skipped".

Damage control against leaked VM cost (see ``conftest.py`` for the full
picture): each test's ``finally`` calls ``mngr destroy --force``; the
``pytest_sessionfinish`` hook force-deletes any VM tagged
``mngr-pytest-launched=true`` older than the TTL; and cloud-init runs
``shutdown -P +N`` (best-effort -- on Azure a stopped VM still bills compute, so
the session-end scanner is the real backstop).

Run manually (the suite takes ~13 minutes, so the duration budget is raised
above ``just test``'s 600s default to avoid a spurious session-time failure):

    AZURE_SUBSCRIPTION_ID=... MNGR_AZURE_RELEASE_TESTS=1 \\
        PYTEST_MAX_DURATION_SECONDS=1200 uv run pytest --no-cov --cov-fail-under=0 \\
        -n 0 -m release \\
        libs/mngr_azure/imbue/mngr_azure/test_release_azure.py
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient

from imbue.mngr_azure.client import AzureVpsClient
from imbue.mngr_azure.config import DEFAULT_IMAGE_OFFER
from imbue.mngr_azure.config import DEFAULT_IMAGE_PUBLISHER
from imbue.mngr_azure.config import DEFAULT_IMAGE_SKU
from imbue.mngr_azure.testing import AZURE_DEFAULT_REGION
from imbue.mngr_azure.testing import AZURE_DEFAULT_RESOURCE_GROUP
from imbue.mngr_azure.testing import AZURE_RELEASE_TESTS_OPT_IN
from imbue.mngr_azure.testing import AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_azure.testing import AZURE_TEST_NAME_PREFIX
from imbue.mngr_azure.testing import AZURE_TEST_VM_SIZE
from imbue.mngr_azure.testing import azure_credentials_available
from imbue.mngr_azure.testing import get_default_subscription_id

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(1200),
    # Skip only when the user did not opt in. Opting in but lacking credentials or
    # a resolvable subscription is a misconfiguration, handled below by
    # ``_fail_if_opted_in_without_credentials`` (credentials) and
    # ``azure_release_subscription_id`` (subscription): they fail loudly rather
    # than skipping, so a release-test run that the user explicitly requested but
    # that cannot reach Azure is visible instead of silently reported as "skipped".
    pytest.mark.skipif(
        not AZURE_RELEASE_TESTS_OPT_IN,
        reason="MNGR_AZURE_RELEASE_TESTS=1 not set",
    ),
]


@pytest.fixture(autouse=True)
def _fail_if_opted_in_without_credentials() -> None:
    """Fail (not skip) when release tests were opted into but Azure credentials are unresolvable.

    The ``skipif`` above has already excluded the not-opted-in case, so reaching
    here means ``MNGR_AZURE_RELEASE_TESTS=1`` is set. If credentials cannot be
    resolved the run is misconfigured -- fail explicitly rather than let the test
    pass as skipped, which would hide that the requested run never executed. (A
    missing subscription is caught with its own message by
    ``azure_release_subscription_id``.)
    """
    if not azure_credentials_available():
        pytest.fail(
            "MNGR_AZURE_RELEASE_TESTS=1 is set but Azure credentials could not be resolved, so the "
            "release tests cannot run. Run `az login` (or set AZURE_CLIENT_ID / AZURE_TENANT_ID / "
            "AZURE_CLIENT_SECRET for a service principal), or unset MNGR_AZURE_RELEASE_TESTS to skip them."
        )


def _write_release_settings(settings_dir: Path, subscription_id: str) -> None:
    """Write the release-test ``settings.toml`` into ``settings_dir``.

    Shared by the prepare fixture and the per-test settings fixture so both the
    ``mngr azure prepare`` and ``mngr create`` subprocesses load the same
    opted-in config. ``is_allowed_in_pytest = true`` is required because the
    subprocesses inherit ``PYTEST_CURRENT_TEST`` and mngr refuses to load any
    config that does not opt in.
    """
    (settings_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        "\n[providers.azure]\n"
        'backend = "azure"\n'
        f'subscription_id = "{subscription_id}"\n'
        f'default_region = "{AZURE_DEFAULT_REGION}"\n'
        f'default_vm_size = "{AZURE_TEST_VM_SIZE}"\n'
        f'resource_group = "{AZURE_DEFAULT_RESOURCE_GROUP}"\n'
        f"auto_shutdown_seconds = {AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        # The test SSH connection from the CI runner / developer laptop needs
        # ingress from any IP.
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n'
        # Disable other remote providers so the create-host preflight doesn't
        # trip on them looking for credentials.
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.aws]\nis_enabled = false\n"
        "\n[providers.gcp]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )


def _real_azure_config_dir() -> str:
    """The developer/CI ``~/.azure`` so the az-CLI credential survives a HOME swap."""
    return os.environ.get("AZURE_CONFIG_DIR") or str(Path.home() / ".azure")


@pytest.fixture(scope="session")
def azure_release_subscription_id() -> str:
    subscription_id = get_default_subscription_id()
    assert subscription_id is not None, (
        "MNGR_AZURE_RELEASE_TESTS=1 is set but no Azure subscription could be resolved, so the "
        "release tests cannot run. Set AZURE_SUBSCRIPTION_ID / MNGR_AZURE_SUBSCRIPTION_ID, run "
        "`az account set --subscription <id>`, or unset MNGR_AZURE_RELEASE_TESTS to skip them."
    )
    return subscription_id


@pytest.fixture(scope="session")
def _azure_release_test_network_prepared(
    tmp_path_factory: pytest.TempPathFactory, azure_release_subscription_id: str
) -> None:
    """Run ``mngr azure prepare`` once per session before any lifecycle test.

    ``create_instance`` resolves (does not create) the subnet on the hot path, so
    the release tests need to run prepare once to create the resource group /
    vnet / subnet / NSG. Runs against an opted-in test ``settings.toml`` and an
    isolated mngr home / HOME so the subprocess doesn't load the developer's real
    mngr profile. ``AZURE_CONFIG_DIR`` is pinned to the real ``~/.azure`` so the
    az-CLI credential keeps resolving after the HOME swap.
    """
    settings_dir = tmp_path_factory.mktemp("azure_prepare_settings")
    _write_release_settings(settings_dir, azure_release_subscription_id)
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    env["MNGR_HOST_DIR"] = str(tmp_path_factory.mktemp("azure_prepare_mngr_home"))
    env["AZURE_CONFIG_DIR"] = _real_azure_config_dir()
    env["HOME"] = str(tmp_path_factory.mktemp("azure_prepare_home"))
    cmd = [
        "uv",
        "run",
        "mngr",
        "azure",
        "prepare",
        "--subscription-id",
        azure_release_subscription_id,
        "--region",
        AZURE_DEFAULT_REGION,
        "--resource-group",
        AZURE_DEFAULT_RESOURCE_GROUP,
        "--allowed-ssh-cidr",
        "0.0.0.0/0",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    assert result.returncode == 0, (
        f"`mngr azure prepare` failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.fixture()
def azure_test_settings_dir(
    tmp_path: Path,
    azure_release_subscription_id: str,
    _azure_release_test_network_prepared: None,
) -> Iterator[Path]:
    """Write a per-test settings.toml selecting Azure with the auto-shutdown TTL."""
    _write_release_settings(tmp_path, azure_release_subscription_id)
    yield tmp_path


def _run_mngr(
    project_config_dir: Path,
    cwd: Path,
    *args: str,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run a mngr command with the test settings.toml in scope.

    Streams stdout+stderr to a log file under ``project_config_dir`` (rather than
    buffering) so a stuck ``mngr create`` still leaves provisioning-phase context
    on a timeout.
    """
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    env["AZURE_CONFIG_DIR"] = _real_azure_config_dir()
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
    azure_test_settings_dir: Path,
    temp_git_repo: Path,
) -> None:
    agent_name = f"{AZURE_TEST_NAME_PREFIX}{int(time.time()) % 100000}"

    result = _run_mngr(
        azure_test_settings_dir,
        temp_git_repo,
        "create",
        agent_name,
        "--type",
        "command",
        "--provider",
        "azure",
        "--no-connect",
        "--",
        "sleep",
        "99999",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}\n--- stdout ---\n{result.stdout}"
    assert "successfully" in result.stdout.lower(), f"unexpected create output: {result.stdout}"

    try:
        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "exec", agent_name, "echo hello-from-azure")
        assert result.returncode == 0, f"Exec failed: {result.stderr}"
        assert "hello-from-azure" in result.stdout

        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "exec", agent_name, "test -d /mngr && echo exists")
        assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
        assert "exists" in result.stdout

        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "list")
        assert result.returncode == 0, f"List failed: {result.stderr}"
        assert agent_name in result.stdout
        assert "azure" in result.stdout
    finally:
        _run_mngr(azure_test_settings_dir, temp_git_repo, "destroy", agent_name, "--force", timeout=180)


@pytest.mark.rsync
def test_provider_lifecycle_create_stop_start_destroy(
    azure_test_settings_dir: Path,
    temp_git_repo: Path,
) -> None:
    agent_name = f"{AZURE_TEST_NAME_PREFIX}ss-{int(time.time()) % 100000}"

    result = _run_mngr(
        azure_test_settings_dir,
        temp_git_repo,
        "create",
        agent_name,
        "--type",
        "command",
        "--provider",
        "azure",
        "--no-connect",
        "--",
        "sleep",
        "99999",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}\n--- stdout ---\n{result.stdout}"
    assert "successfully" in result.stdout.lower(), f"unexpected create output: {result.stdout}"

    try:
        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "stop", agent_name)
        assert result.returncode == 0, f"Stop failed: {result.stderr}"

        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "list")
        assert result.returncode == 0
        assert agent_name in result.stdout

        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "start", agent_name, "--no-connect")
        assert result.returncode == 0, f"Start failed: {result.stderr}"

        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "exec", agent_name, "echo alive-after-restart")
        assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
        assert "alive-after-restart" in result.stdout
    finally:
        _run_mngr(azure_test_settings_dir, temp_git_repo, "destroy", agent_name, "--force", timeout=180)


@pytest.mark.rsync
def test_provider_create_builds_dockerfile_on_vm(
    azure_test_settings_dir: Path,
    temp_git_repo: Path,
) -> None:
    """Azure builds a project Dockerfile on the VM and runs the agent from it.

    The other lifecycle tests create from the default base image; this one exercises
    the remote-build path the ``-t azure`` template relies on (the same shared
    ``mngr_vps_docker`` flow ``gcp`` uses): ``mngr create`` uploads the build context,
    runs ``docker build`` on the VM, and starts the agent container FROM the built
    image. We confirm that by baking a marker into the image with a ``RUN`` and reading
    it back via ``exec`` -- if create silently fell back to the base image, the marker
    would be absent.

    Uses the default ``DOCKER`` builder (native ``docker build`` on the VM; no
    DEPOT_TOKEN needed) and a tiny Dockerfile, so the test stays fast and self-contained.
    The marker stands in for any Dockerfile-installed content -- e.g. ``gh`` and the rest
    of the mngr toolchain in the real image -- whose contents are already build-tested by
    the docker/modal CI image builds; what is azure-specific, and untested until now, is
    the build-on-VM integration itself.
    """
    marker = "azure-dockerfile-build-ok"
    (temp_git_repo / "Dockerfile").write_text(
        f"FROM debian:bookworm-slim\nRUN echo {marker} > /azure-build-marker.txt\n"
    )
    # mngr create refuses an unclean working tree, so commit the Dockerfile (the
    # normal case: a tracked Dockerfile built from the worktree).
    subprocess.run(["git", "-C", str(temp_git_repo), "add", "Dockerfile"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(temp_git_repo), "commit", "-q", "-m", "add test Dockerfile"],
        check=True,
        capture_output=True,
    )
    agent_name = f"{AZURE_TEST_NAME_PREFIX}build-{int(time.time()) % 100000}"

    result = _run_mngr(
        azure_test_settings_dir,
        temp_git_repo,
        "create",
        agent_name,
        "--type",
        "command",
        "--provider",
        "azure",
        # D2s_v3 because B-series is currently NotAvailableForSubscription in westus.
        "-b",
        "--azure-vm-size=Standard_D2s_v3",
        "-b",
        "--file=Dockerfile",
        "-b",
        ".",
        "--no-connect",
        "--",
        "sleep",
        "99999",
        timeout=900,
    )
    assert result.returncode == 0, (
        f"Create (with Dockerfile build) failed: {result.stderr}\n--- stdout ---\n{result.stdout}"
    )
    assert "successfully" in result.stdout.lower(), f"unexpected create output: {result.stdout}"

    try:
        result = _run_mngr(azure_test_settings_dir, temp_git_repo, "exec", agent_name, "cat /azure-build-marker.txt")
        assert result.returncode == 0, f"Exec failed: {result.stderr}\n--- stdout ---\n{result.stdout}"
        assert marker in result.stdout, (
            "Dockerfile build marker missing -- the agent container was NOT built from the "
            f"provided Dockerfile (silent fall-back to the base image?). Output: {result.stdout}"
        )
    finally:
        _run_mngr(azure_test_settings_dir, temp_git_repo, "destroy", agent_name, "--force", timeout=180)


# =============================================================================
# API client smoke tests (real network calls, read-only)
# =============================================================================


@pytest.fixture()
def azure_release_client(azure_release_subscription_id: str) -> AzureVpsClient:
    """Real Azure API client for release-test read-only calls."""
    return AzureVpsClient(
        credential=DefaultAzureCredential(),
        subscription_id=azure_release_subscription_id,
        region=AZURE_DEFAULT_REGION,
        resource_group=AZURE_DEFAULT_RESOURCE_GROUP,
    )


def test_api_client_list_instances_does_not_error(azure_release_client: AzureVpsClient) -> None:
    instances = azure_release_client.list_instances()
    assert isinstance(instances, list)


def test_default_image_resolves(azure_release_subscription_id: str) -> None:
    """The default Ubuntu marketplace image must still resolve via the Compute API.

    Marketplace SKUs occasionally change names; a periodic release-test run is
    the cheapest way to catch a stale default before it breaks every create.
    """
    compute = ComputeManagementClient(DefaultAzureCredential(), azure_release_subscription_id)
    versions = list(
        compute.virtual_machine_images.list(
            location=AZURE_DEFAULT_REGION,
            publisher_name=DEFAULT_IMAGE_PUBLISHER,
            offer=DEFAULT_IMAGE_OFFER,
            skus=DEFAULT_IMAGE_SKU,
        )
    )
    assert versions, (
        f"Default image {DEFAULT_IMAGE_PUBLISHER}:{DEFAULT_IMAGE_OFFER}:{DEFAULT_IMAGE_SKU} resolved no versions in "
        f"{AZURE_DEFAULT_REGION}. The marketplace SKU may have changed; update the image_* defaults in config.py."
    )
