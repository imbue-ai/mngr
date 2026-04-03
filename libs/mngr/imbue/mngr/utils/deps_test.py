import platform

import pytest

from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.utils.deps import SystemDependency

_EXISTING_BINARY = SystemDependency(
    binary="python3",
    purpose="testing",
    macos_hint="brew install python3",
    linux_hint="sudo apt-get install python3",
)

_MISSING_BINARY = SystemDependency(
    binary="definitely-not-a-real-binary-xyz",
    purpose="testing",
    macos_hint="brew install xyz",
    linux_hint="apt-get install xyz",
)


def test_system_dependency_is_available_for_existing_binary() -> None:
    """is_available returns True for a binary known to exist."""
    assert _EXISTING_BINARY.is_available() is True


def test_system_dependency_is_available_for_missing_binary() -> None:
    """is_available returns False for a nonexistent binary."""
    assert _MISSING_BINARY.is_available() is False


def test_system_dependency_require_passes_for_existing_binary() -> None:
    """require does not raise for a binary that exists."""
    _EXISTING_BINARY.require()


def test_system_dependency_require_raises_for_missing_binary() -> None:
    """require raises BinaryNotInstalledError with correct fields."""
    dep = SystemDependency(
        binary="definitely-not-a-real-binary-xyz",
        purpose="unit testing",
        macos_hint="brew install xyz",
        linux_hint="apt-get install xyz",
    )
    with pytest.raises(BinaryNotInstalledError) as exc_info:
        dep.require()

    err = exc_info.value
    assert dep.binary in str(err)
    assert "unit testing" in str(err)


def test_install_hint_returns_platform_specific_hint() -> None:
    """install_hint returns the correct hint for the current platform."""
    dep = SystemDependency(
        binary="python3",
        purpose="testing",
        macos_hint="use brew",
        linux_hint="use apt",
    )
    if platform.system() == "Darwin":
        assert dep.install_hint == "use brew"
    else:
        assert dep.install_hint == "use apt"
