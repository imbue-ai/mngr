"""Unit tests for the slow-path rebuild provider/config builders."""

from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.providers.rebuild import _build_delegated_vps_config
from imbue.mngr_imbue_cloud.providers.rebuild import _slice_memory_mib_from_lease_attributes


def test_build_delegated_vps_config_forwards_runtime_knobs() -> None:
    """The slow-path rebuild must carry runsc + hardening args onto the vps_docker config."""
    config = ImbueCloudProviderConfig(
        account=ImbueCloudAccount("a@b.com"),
        docker_runtime="runsc",
        install_gvisor_runtime=True,
        default_start_args=("--workdir=/", "--security-opt=no-new-privileges"),
    )
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.backend == "vps_docker"
    assert vps_config.docker_runtime == "runsc"
    assert vps_config.install_gvisor_runtime is True
    assert vps_config.default_start_args == ("--workdir=/", "--security-opt=no-new-privileges")
    # The connection-shape fields are still forwarded from the imbue_cloud config.
    assert vps_config.host_dir == config.host_dir
    assert vps_config.container_ssh_port == config.container_ssh_port


def test_build_delegated_vps_config_defaults_to_no_runtime() -> None:
    """With an unconfigured imbue_cloud config, no runtime is forced (runc)."""
    config = ImbueCloudProviderConfig(account=ImbueCloudAccount("a@b.com"))
    vps_config = _build_delegated_vps_config(config)
    assert vps_config.docker_runtime is None
    assert vps_config.install_gvisor_runtime is False
    assert vps_config.default_start_args == ()


def test_slice_memory_mib_from_lease_attributes_converts_the_stamped_memory_gb() -> None:
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": 8, "cpus": 2}) == 8192


def test_slice_memory_mib_from_lease_attributes_returns_none_for_missing_or_unusable_values() -> None:
    # A legacy row without the stamp, or a malformed value, must yield None (no
    # cap) rather than a bogus cap or a crash mid-create.
    assert _slice_memory_mib_from_lease_attributes({}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": "8"}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": True}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": 0}) is None
    assert _slice_memory_mib_from_lease_attributes({"memory_gb": -4}) is None
