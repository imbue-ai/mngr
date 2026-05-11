import pytest

from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_sbx.errors import SbxCommandError
from imbue.mngr_sbx.errors import SbxHostCreationError
from imbue.mngr_sbx.errors import SbxHostRenameError
from imbue.mngr_sbx.errors import SbxNotAuthorizedError
from imbue.mngr_sbx.errors import SbxNotInstalledError


def test_sbx_not_installed_inherits_provider_unavailable() -> None:
    error = SbxNotInstalledError(ProviderInstanceName("sbx"))
    assert isinstance(error, ProviderUnavailableError)
    assert "sbx is not installed" in str(error)


def test_sbx_not_authorized_inherits_provider_not_authorized_and_mentions_login() -> None:
    error = SbxNotAuthorizedError(ProviderInstanceName("sbx"))
    assert isinstance(error, ProviderNotAuthorizedError)
    assert "sbx login" in str(error)


def test_sbx_command_error_records_returncode_and_stderr() -> None:
    error = SbxCommandError("ls", returncode=2, stderr="boom")
    assert error.command == "ls"
    assert error.returncode == 2
    assert error.stderr == "boom"
    assert "sbx ls" in str(error)
    assert "boom" in str(error)


def test_sbx_host_creation_error_inherits_host_creation_error() -> None:
    error = SbxHostCreationError("could not start sandbox")
    assert isinstance(error, HostCreationError)
    assert "could not start sandbox" in str(error)


def test_sbx_host_rename_error_inherits_mngr_error() -> None:
    error = SbxHostRenameError()
    assert isinstance(error, MngrError)
    assert "cannot be renamed" in str(error)


def test_sbx_not_authorized_is_subclass_of_mngr_error() -> None:
    # Sanity check that callers catching MngrError also catch this typed error.
    with pytest.raises(MngrError):
        raise SbxNotAuthorizedError(ProviderInstanceName("sbx"))
