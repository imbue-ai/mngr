from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_lima.errors import LimaHostCreationError
from imbue.mngr_lima.errors import LimaHostRenameError
from imbue.mngr_lima.errors import LimaNotInstalledError
from imbue.mngr_lima.errors import LimaVersionError


def test_lima_not_installed_error() -> None:
    error = LimaNotInstalledError(ProviderInstanceName("lima"))
    assert isinstance(error, ProviderUnavailableError)
    assert "limactl" in str(error)
    assert "not installed" in str(error)


def test_lima_version_error() -> None:
    error = LimaVersionError(ProviderInstanceName("lima"), "0.9.0", "1.0.0")
    assert isinstance(error, ProviderUnavailableError)
    assert "0.9.0" in str(error)
    assert "1.0.0" in str(error)


def test_lima_command_error() -> None:
    error = LimaCommandError("start", 1, "some error")
    assert isinstance(error, MngrError)
    assert error.command == "start"
    assert error.returncode == 1
    assert "some error" in str(error)


def test_lima_host_creation_error() -> None:
    error = LimaHostCreationError("disk full")
    assert isinstance(error, HostCreationError)
    assert "disk full" in str(error)


def test_lima_host_rename_error() -> None:
    error = LimaHostRenameError()
    assert isinstance(error, MngrError)
    assert "cannot be renamed" in str(error)
