import pytest

from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderError
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import assert_init_first_param_is_provider_name
from imbue.mngr.utils.testing import walk_concrete_subclasses
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
    error = LimaHostCreationError(ProviderInstanceName("lima"), "disk full")
    assert isinstance(error, HostCreationError)
    assert error.provider_name == ProviderInstanceName("lima")
    assert "disk full" in str(error)


def test_lima_host_rename_error() -> None:
    error = LimaHostRenameError()
    assert isinstance(error, MngrError)
    assert "cannot be renamed" in str(error)


_LIMA_PROVIDER_ERROR_SUBCLASSES = [
    cls for cls in walk_concrete_subclasses(ProviderError) if cls.__module__.startswith("imbue.mngr_lima")
]

# Fail loudly at collection time if no Lima ProviderError subclasses were
# discovered. Otherwise pytest silently parametrizes zero cases and the
# invariant test passes without enforcing anything.
assert _LIMA_PROVIDER_ERROR_SUBCLASSES, (
    "No Lima ProviderError subclasses discovered via walk_concrete_subclasses. "
    "Ensure the modules defining them are imported by this test file."
)


@pytest.mark.parametrize("subclass", _LIMA_PROVIDER_ERROR_SUBCLASSES, ids=lambda c: c.__name__)
def test_lima_provider_error_subclass_takes_provider_name_first(subclass: type) -> None:
    """Every Lima ProviderError subclass must accept provider_name as its first parameter.

    Mirrors the same invariant enforced for mngr's own ProviderError subclasses
    in mngr/errors_test.py, scoped to subclasses defined in this package so
    handlers that catch ProviderError can rely on e.provider_name.
    """
    assert_init_first_param_is_provider_name(subclass)
