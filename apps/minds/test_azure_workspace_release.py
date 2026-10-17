"""End-to-end release test for the minds Azure (bring-your-own-key) compute provider.

Provisions a real Azure VM through a minds-shaped BYOK Azure account block
(``[providers.byok-azure-<slug>]``, the shape ``set_cloud_account_provider``
writes) and asserts the agent container has the shape minds relies on: a runsc
(gVisor) hardened Docker container on the Azure VM *outer* host, with /run and
/tmp on tmpfs and a memory cap derived from the VM's RAM.

This provisions and destroys a real Azure VM, so it costs real money. It is
double-gated, matching ``mngr_azure``'s release tests:

- Azure credentials must resolve (a service principal via ``AZURE_CLIENT_ID`` /
  ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_SECRET`` plus ``AZURE_SUBSCRIPTION_ID``).
- ``MNGR_AZURE_RELEASE_TESTS=1`` must be set.

Run manually:

    MNGR_AZURE_RELEASE_TESTS=1 MNGR_AZURE_REGION=westus MNGR_AZURE_RESOURCE_GROUP=mngr \\
        just test apps/minds/test_azure_workspace_release.py

``mngr azure prepare`` must already have created the resource group's network
and storage account (the test re-runs it, which is a no-op when they exist).
"""

import os
import uuid
from pathlib import Path

import pytest

from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.testing import assert_cloud_workspace_container_shape
from imbue.minds.testing import make_cloud_workspace_release_env
from imbue.minds.testing import run_mngr_for_cloud_workspace_release_test
from imbue.mngr_azure.testing import AZURE_DEFAULT_REGION
from imbue.mngr_azure.testing import AZURE_DEFAULT_RESOURCE_GROUP
from imbue.mngr_azure.testing import AZURE_RELEASE_TESTS_OPT_IN
from imbue.mngr_azure.testing import AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_azure.testing import AZURE_TEST_VM_SIZE
from imbue.mngr_azure.testing import azure_credentials_available

# Marked rsync for the same reason as the AWS twin: ``mngr create`` pushes mngr's
# prerequisites to the VM over rsync.
pytestmark = [
    pytest.mark.release,
    pytest.mark.rsync,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (azure_credentials_available() and AZURE_RELEASE_TESTS_OPT_IN),
        reason="Azure credentials or MNGR_AZURE_RELEASE_TESTS=1 not set",
    ),
]

# The same ``byok-azure-<slug>`` naming minds gives an account block, so the
# create address (``...@<host>.byok-azure-release``) selects exactly this block.
_AZURE_PROVIDER_NAME = "byok-azure-release"
_REGION = AZURE_DEFAULT_REGION


@pytest.fixture()
def azure_release_env(tmp_path: Path) -> dict[str, str]:
    """The subprocess env for the release test: the BYOK Azure provider block exactly as minds writes it.

    Backend ``azure``, the region, VM size and resource group, the user-data
    layout, the runsc hardening knobs, plus the release-test-only
    ``auto_shutdown_seconds`` safety net the AzureProvider requires under pytest;
    the service-principal credentials and subscription pass through the
    environment untouched.
    """
    return make_cloud_workspace_release_env(
        tmp_path,
        f"\n[providers.{_AZURE_PROVIDER_NAME}]\n"
        'backend = "azure"\n'
        f'default_region = "{_REGION}"\n'
        f'resource_group = "{AZURE_DEFAULT_RESOURCE_GROUP}"\n'
        f'default_vm_size = "{AZURE_TEST_VM_SIZE}"\n'
        f'host_dir = "{WORKSPACE_HOST_DIR}"\n'
        f'volume_home_path = "{WORKSPACE_VOLUME_HOME_PATH}"\n'
        f'host_log_dir = "{WORKSPACE_HOST_LOG_DIR}"\n'
        "install_gvisor_runtime = true\n"
        'docker_runtime = "runsc"\n'
        f"auto_shutdown_seconds = {AZURE_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS}\n"
        'allowed_ssh_cidrs = ["0.0.0.0/0"]\n',
        os.environ,
    )


def test_azure_workspace_runs_in_runsc_container_on_azure_vm(
    azure_release_env: dict[str, str], temp_git_repo: Path
) -> None:
    """A minds BYOK Azure account block lands a runsc-hardened, tmpfs-mounted, memory-capped container on a real Azure VM."""
    host_name = f"test-azure-mind-{uuid.uuid4().hex}"
    agent_address = f"agent@{host_name}.{_AZURE_PROVIDER_NAME}"

    prepare = run_mngr_for_cloud_workspace_release_test(
        azure_release_env, temp_git_repo, "azure", "prepare", "--provider", _AZURE_PROVIDER_NAME
    )
    assert prepare.returncode == 0, f"azure prepare failed:\n{prepare.stdout}"

    create = run_mngr_for_cloud_workspace_release_test(
        azure_release_env,
        temp_git_repo,
        "create",
        agent_address,
        "--new-host",
        "--type",
        "command",
        "--no-connect",
        "-b",
        f"--azure-region={_REGION}",
        "--",
        "sleep",
        "99999",
    )
    assert create.returncode == 0, f"Azure create failed:\n{create.stdout}"

    try:
        assert_cloud_workspace_container_shape(
            azure_release_env,
            temp_git_repo,
            agent_address=agent_address,
            host_name=host_name,
            provider_name=_AZURE_PROVIDER_NAME,
        )
    finally:
        run_mngr_for_cloud_workspace_release_test(
            azure_release_env, temp_git_repo, "destroy", agent_address, "--force"
        )
