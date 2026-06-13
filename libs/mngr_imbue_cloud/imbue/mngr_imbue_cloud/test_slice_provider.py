import os
import pwd
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.logging import register_build_level
from imbue.mngr_imbue_cloud.bare_metal import slice_lima_instance_name
from imbue.mngr_imbue_cloud.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.slice_provider import SliceVpsDockerProviderConfig
from imbue.mngr_vps_docker.primitives import VpsInstanceId

# limactl's output streamer logs at the custom BUILD level; ensure it's registered
# when this test runs outside the full mngr CLI bootstrap.
register_build_level()


@pytest.mark.release
# Booting a real VM + baking a container far exceeds the package's 10s default timeout.
@pytest.mark.timeout(1800)
@pytest.mark.skipif(shutil.which("limactl") is None, reason="requires limactl + a hypervisor")
def test_slice_provider_bakes_a_reachable_host_on_a_real_lima_vm(
    temp_mngr_ctx: MngrContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: carve a lima slice, bake the vps_docker container, reach it, tear down.

    Boots a real lima VM, so this is a (slow) release test. It exercises the whole
    slice path: provision the VPS-parity VM, run the shared container bake against
    it over box-forwarded ports, confirm the resulting host's inner container is
    reachable, then destroy the VM + disk.
    """
    # temp_mngr_ctx overrides $HOME to a deep pytest tmp dir; lima then splits
    # across $HOME (deep) and LIMA_HOME and its instance/socket paths break (the
    # 108-char UNIX socket limit, and an uninitialized home). Restore the real,
    # short, already-initialized home for lima -- which is also what production
    # uses on the box. mngr_ctx already captured its (deep) profile dir at
    # construction, so this only affects lima. The real home is read via pwd
    # because $HOME has been overridden.
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)
    monkeypatch.setenv("LIMA_HOME", str(Path(real_home) / ".lima"))

    backend = ProviderBackendName("imbue_cloud_slice")
    config = SliceVpsDockerProviderConfig(
        backend=backend,
        # Small VM so the test boots quickly.
        slice_vcpus=2,
        slice_memory_mib=2048,
        slice_disk_gib=10,
    )
    client = LimaSliceVpsClient()
    provider = SliceVpsDockerProvider(
        name=ProviderInstanceName("test-slice"),
        # host_dir is the in-container mngr dir (/mngr), NOT a host-side path --
        # the container is a fresh image, so a deep host path wouldn't exist there.
        host_dir=config.host_dir,
        mngr_ctx=temp_mngr_ctx,
        config=config,
        vps_client=client,
        slice_config=config,
        lima_client=client,
    )

    host_name = HostName(f"slice-test-{uuid4().hex}")
    host = provider.create_host(name=host_name)
    instance_id = VpsInstanceId(slice_lima_instance_name(host.id))
    try:
        # The returned host is the inner container, reached via the box-forwarded
        # container port. A successful command proves the full chain works:
        # VM provisioned -> docker container baked -> sshd reachable over the forward.
        result = host.execute_idempotent_command("echo slice-ok", timeout_seconds=60.0)
        assert result.success
        assert "slice-ok" in result.stdout
    finally:
        client.destroy_instance(instance_id)
