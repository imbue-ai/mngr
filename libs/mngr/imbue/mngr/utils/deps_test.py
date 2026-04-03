import platform

import pytest

from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.deps import ALL_DEPS
from imbue.mngr.utils.deps import CORE_DEPS
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import InstallMethod
from imbue.mngr.utils.deps import OPTIONAL_DEPS
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import SystemDependency
from imbue.mngr.utils.deps import check_bash_version
from imbue.mngr.utils.deps import detect_os
from imbue.mngr.utils.deps import install_deps_batch

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


# -- OsName / DependencyCategory / InstallMethod --


def test_detect_os_returns_valid_os_name() -> None:
    """detect_os returns MACOS on Darwin and LINUX on Linux."""
    os_name = detect_os()
    if platform.system() == "Darwin":
        assert os_name == OsName.MACOS
    else:
        assert os_name == OsName.LINUX


def test_dependency_category_values() -> None:
    """DependencyCategory has CORE and OPTIONAL members."""
    assert DependencyCategory.CORE == "CORE"
    assert DependencyCategory.OPTIONAL == "OPTIONAL"


def test_install_method_construction() -> None:
    """InstallMethod can be constructed with all fields."""
    method = InstallMethod(brew_package="foo", apt_package="bar", custom_install_script="https://example.com")
    assert method.brew_package == "foo"
    assert method.apt_package == "bar"
    assert method.custom_install_script == "https://example.com"


def test_install_method_defaults_to_none() -> None:
    """InstallMethod fields default to None."""
    method = InstallMethod()
    assert method.brew_package is None
    assert method.apt_package is None
    assert method.custom_install_script is None


# -- Dependency catalog --


def test_all_deps_is_union_of_core_and_optional() -> None:
    """ALL_DEPS is exactly CORE_DEPS + OPTIONAL_DEPS."""
    assert ALL_DEPS == CORE_DEPS + OPTIONAL_DEPS


def test_core_deps_have_core_category() -> None:
    """All CORE_DEPS have category CORE."""
    for dep in CORE_DEPS:
        assert dep.category == DependencyCategory.CORE, f"{dep.binary} should be CORE"


def test_optional_deps_have_optional_category() -> None:
    """All OPTIONAL_DEPS have category OPTIONAL."""
    for dep in OPTIONAL_DEPS:
        assert dep.category == DependencyCategory.OPTIONAL, f"{dep.binary} should be OPTIONAL"


def test_all_deps_have_install_method() -> None:
    """All defined system dependencies have an install_method set."""
    for dep in ALL_DEPS:
        assert dep.install_method is not None, f"{dep.binary} should have an install_method"


# -- check_bash_version --


def test_check_bash_version_returns_bool() -> None:
    """check_bash_version returns a boolean."""
    result = check_bash_version()
    assert isinstance(result, bool)


def test_check_bash_version_with_minimum_1_returns_true() -> None:
    """check_bash_version with minimum=1 should return True on any system with bash."""
    assert check_bash_version(minimum=1) is True


# -- install_deps_batch --


def test_install_deps_batch_with_empty_list_returns_empty() -> None:
    """install_deps_batch with no deps returns no failures."""
    os_name = detect_os()
    result = install_deps_batch([], os_name)
    assert result == []


def test_install_deps_batch_skips_deps_without_install_method() -> None:
    """Deps with install_method=None are silently skipped (not reported as failed)."""
    dep = SystemDependency(
        binary="no-such-binary-xyz",
        purpose="testing",
        macos_hint="n/a",
        linux_hint="n/a",
        install_method=None,
    )
    os_name = detect_os()
    result = install_deps_batch([dep], os_name)
    assert result == []


# -- detect_os edge case --


def test_detect_os_raises_on_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """detect_os raises MngrError on unsupported platforms."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    with pytest.raises(MngrError, match="Unsupported operating system"):
        detect_os()
