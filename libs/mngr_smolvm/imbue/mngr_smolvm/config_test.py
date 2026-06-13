from pathlib import Path

from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.constants import DEFAULT_CPUS
from imbue.mngr_smolvm.constants import DEFAULT_HOST_DATA_DISK_SIZE_GB
from imbue.mngr_smolvm.constants import DEFAULT_MEMORY_MIB
from imbue.mngr_smolvm.constants import SMOLVM_BACKEND_NAME


def test_config_defaults() -> None:
    config = SmolvmProviderConfig()
    assert config.backend == SMOLVM_BACKEND_NAME
    assert config.smolvm_command == "smolvm"
    assert config.is_host_data_volume_exposed is True
    assert config.host_data_disk_size_gb == DEFAULT_HOST_DATA_DISK_SIZE_GB
    assert config.default_cpus == DEFAULT_CPUS
    assert config.default_memory_mib == DEFAULT_MEMORY_MIB


def test_config_btrfs_layout() -> None:
    config = SmolvmProviderConfig(
        is_host_data_volume_exposed=False,
        host_data_disk_size_gb=50,
    )
    assert config.is_host_data_volume_exposed is False
    assert config.host_data_disk_size_gb == 50


def test_config_custom_command_and_host_dir() -> None:
    config = SmolvmProviderConfig(
        smolvm_command="/opt/smolvm/bin/smolvm",
        host_dir=Path("/data/mngr"),
    )
    assert config.smolvm_command == "/opt/smolvm/bin/smolvm"
    assert config.host_dir == Path("/data/mngr")
