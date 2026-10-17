"""End-to-end release test for the minds GCP (bring-your-own-key) compute provider.

Provisions a real GCE instance through a minds-shaped BYOK GCP account block
(``[providers.byok-gcp-<slug>]``, the shape ``set_cloud_account_provider``
writes) and asserts the agent container has the shape minds relies on: a runsc
(gVisor) hardened Docker container on the GCE *outer* host, with /run and /tmp
on tmpfs and a memory cap derived from the VM's RAM.

This provisions and destroys a real GCE instance, so it costs real money. It is
double-gated, matching ``mngr_gcp``'s release tests:

- Google Application Default Credentials must resolve (``gcloud auth
  application-default login``, or ``GOOGLE_APPLICATION_CREDENTIALS``), with a
  project from ``MNGR_GCP_PROJECT`` or the ADC.
- ``MNGR_GCP_RELEASE_TESTS=1`` must be set.

Run manually:

    MNGR_GCP_RELEASE_TESTS=1 MNGR_GCP_PROJECT=<project> \\
        just test apps/minds/test_gcp_workspace_release.py

``mngr gcp prepare`` must already have created the project's SSH firewall rule
(the test re-runs it, which is a read-only no-op when it exists).
"""

import os
import pwd
import uuid
from pathlib import Path

import pytest

from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.primitives import DEFAULT_GCP_MACHINE_TYPE
from imbue.minds.testing import assert_cloud_workspace_container_shape
from imbue.minds.testing import make_cloud_workspace_release_env
from imbue.minds.testing import run_mngr_for_cloud_workspace_release_test
from imbue.mngr_gcp.testing import GCP_DEFAULT_ZONE
from imbue.mngr_gcp.testing import GCP_RELEASE_TESTS_OPT_IN
from imbue.mngr_gcp.testing import GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_gcp.testing import gcp_credentials_available
from imbue.mngr_gcp.testing import get_default_project

# Marked rsync for the same reason as the AWS twin: ``mngr create`` pushes mngr's
# prerequisites to the VM over rsync.
pytestmark = [
    pytest.mark.release,
    pytest.mark.rsync,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (gcp_credentials_available() and GCP_RELEASE_TESTS_OPT_IN),
        reason="GCP credentials or MNGR_GCP_RELEASE_TESTS=1 not set",
    ),
]

# The same ``byok-gcp-<slug>`` naming minds gives an account block, so the create
# address (``...@<host>.byok-gcp-release``) selects exactly this block.
_GCP_PROVIDER_NAME = "byok-gcp-release"
_ZONE = GCP_DEFAULT_ZONE

# Where google.auth looks for the gcloud ADC file, relative to the real home.
_GCLOUD_CONFIG_RELATIVE_PATH = Path(".config/gcloud")


@pytest.fixture()
def gcp_release_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The subprocess env for the release test: the BYOK GCP provider block exactly as minds writes it.

    Backend ``gcp``, the project, zone and machine type, the user-data layout,
    the runsc hardening knobs, plus the release-test-only ``auto_shutdown_seconds``
    safety net the GcpProvider requires under pytest. The minds autouse fixtures
    have already swapped HOME by the time this runs, which hides the gcloud ADC
    file from both this process and the mngr subprocess, so when no explicit
    ``GOOGLE_APPLICATION_CREDENTIALS`` is set the real gcloud config directory is
    pinned through ``CLOUDSDK_CONFIG`` (the same trick the gcp plugin's own
    conftest uses) before the project is resolved.
    """
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and "CLOUDSDK_CONFIG" not in os.environ:
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(real_home / _GCLOUD_CONFIG_RELATIVE_PATH))
    project = get_default_project()
    assert project is not None, "a GCP project must resolve (release-test skipif guards this)"
    return make_cloud_workspace_release_env(
        tmp_path,
        f"\n[providers.{_GCP_PROVIDER_NAME}]\n"
        'backend = "gcp"\n'
        f'project_id = "{project}"\n'
        f'default_zone = "{_ZONE}"\n'
        f'default_machine_type = "{DEFAULT_GCP_MACHINE_TYPE}"\n'
        f'host_dir = "{WORKSPACE_HOST_DIR}"\n'
        f'volume_home_path = "{WORKSPACE_VOLUME_HOME_PATH}"\n'
        f'host_log_dir = "{WORKSPACE_HOST_LOG_DIR}"\n'
        "install_gvisor_runtime = true\n"
        'docker_runtime = "runsc"\n'
        f"auto_shutdown_seconds = {GCP_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n',
        os.environ,
    )


def test_gcp_workspace_runs_in_runsc_container_on_gce(gcp_release_env: dict[str, str], temp_git_repo: Path) -> None:
    """A minds BYOK GCP account block lands a runsc-hardened, tmpfs-mounted, memory-capped container on a real GCE outer host."""
    host_name = f"test-gcp-mind-{uuid.uuid4().hex}"
    agent_address = f"agent@{host_name}.{_GCP_PROVIDER_NAME}"

    prepare = run_mngr_for_cloud_workspace_release_test(
        gcp_release_env, temp_git_repo, "gcp", "prepare", "--provider", _GCP_PROVIDER_NAME
    )
    assert prepare.returncode == 0, f"gcp prepare failed:\n{prepare.stdout}"

    create = run_mngr_for_cloud_workspace_release_test(
        gcp_release_env,
        temp_git_repo,
        "create",
        agent_address,
        "--new-host",
        "--type",
        "command",
        "--no-connect",
        "-b",
        f"--gcp-zone={_ZONE}",
        "--",
        "sleep",
        "99999",
    )
    assert create.returncode == 0, f"GCP create failed:\n{create.stdout}"

    try:
        assert_cloud_workspace_container_shape(
            gcp_release_env,
            temp_git_repo,
            agent_address=agent_address,
            host_name=host_name,
            provider_name=_GCP_PROVIDER_NAME,
        )
    finally:
        run_mngr_for_cloud_workspace_release_test(gcp_release_env, temp_git_repo, "destroy", agent_address, "--force")
