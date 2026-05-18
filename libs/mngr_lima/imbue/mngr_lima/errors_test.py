import inspect

import pytest

from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderError
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
    error = LimaHostCreationError(ProviderInstanceName("lima"), "disk full")
    assert isinstance(error, HostCreationError)
    assert error.provider_name == ProviderInstanceName("lima")
    assert "disk full" in str(error)


def test_lima_host_rename_error() -> None:
    error = LimaHostRenameError()
    assert isinstance(error, MngrError)
    assert "cannot be renamed" in str(error)


def _walk_concrete_subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        if not inspect.isabstract(sub):
            found.append(sub)
        found.extend(_walk_concrete_subclasses(sub))
    return found


_LIMA_PROVIDER_ERROR_SUBCLASSES = [
    cls for cls in _walk_concrete_subclasses(ProviderError) if cls.__module__.startswith("imbue.mngr_lima")
]


@pytest.mark.parametrize("subclass", _LIMA_PROVIDER_ERROR_SUBCLASSES, ids=lambda c: c.__name__)
def test_lima_provider_error_subclass_takes_provider_name_first(subclass: type) -> None:
    """Every Lima ProviderError subclass must accept provider_name as its first parameter.

    Mirrors the same invariant enforced for mngr's own ProviderError subclasses
    in mngr/errors_test.py, scoped to subclasses defined in this package so
    handlers that catch ProviderError can rely on e.provider_name.
    """
    params = list(inspect.signature(subclass.__init__).parameters.values())
    assert len(params) >= 2, f"{subclass.__name__}.__init__ has no parameters beyond self"
    assert params[1].name == "provider_name", (
        f"{subclass.__name__}.__init__ first parameter is {params[1].name!r}, expected 'provider_name'"
    )
    assert params[1].annotation is ProviderInstanceName, (
        f"{subclass.__name__}.__init__ provider_name annotation is {params[1].annotation!r}, "
        f"expected ProviderInstanceName"
    )
