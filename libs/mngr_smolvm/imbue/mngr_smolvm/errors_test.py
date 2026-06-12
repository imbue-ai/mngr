import pytest

from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm.errors import SmolvmCapabilityError
from imbue.mngr_smolvm.errors import SmolvmCommandError
from imbue.mngr_smolvm.errors import SmolvmHostCreationError
from imbue.mngr_smolvm.errors import SmolvmHostRenameError
from imbue.mngr_smolvm.errors import SmolvmNotInstalledError
from imbue.mngr_smolvm.errors import SmolvmProvisioningError
from imbue.mngr_smolvm.errors import SmolvmVersionError


def test_not_installed_error_mentions_command() -> None:
    error = SmolvmNotInstalledError(ProviderInstanceName("smolvm"), "smolvm-custom")
    assert isinstance(error, ProviderUnavailableError)
    assert "smolvm-custom" in str(error)


def test_version_error_mentions_versions() -> None:
    error = SmolvmVersionError(ProviderInstanceName("smolvm"), "1.0.0", "1.0.3")
    assert isinstance(error, ProviderUnavailableError)
    assert "1.0.0" in str(error)
    assert "1.0.3" in str(error)


def test_capability_error_mentions_capability() -> None:
    error = SmolvmCapabilityError(ProviderInstanceName("smolvm"), "persistent data disks (--data-disk)")
    assert isinstance(error, ProviderUnavailableError)
    assert "--data-disk" in str(error)
    assert "btrfs" in str(error)


def test_command_error_includes_details() -> None:
    error = SmolvmCommandError("machine start", 1, "boom")
    assert isinstance(error, MngrError)
    assert error.command == "machine start"
    assert error.returncode == 1
    assert error.stderr == "boom"
    assert "machine start" in str(error)
    assert "boom" in str(error)


def test_host_creation_error_is_host_creation_error() -> None:
    error = SmolvmHostCreationError(ProviderInstanceName("smolvm"), "no kvm")
    assert isinstance(error, HostCreationError)
    assert "no kvm" in str(error)


def test_rename_error_explains_workaround() -> None:
    error = SmolvmHostRenameError()
    assert isinstance(error, MngrError)
    assert "renamed" in str(error)


def test_provisioning_error_mentions_machine() -> None:
    error = SmolvmProvisioningError("mngr-host-1", "apk failed")
    assert isinstance(error, MngrError)
    assert "mngr-host-1" in str(error)
    assert "apk failed" in str(error)


def test_provisioning_error_can_be_raised() -> None:
    with pytest.raises(SmolvmProvisioningError):
        raise SmolvmProvisioningError("m", "r")
