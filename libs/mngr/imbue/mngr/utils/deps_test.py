import platform

import pytest

from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.errors import MngrError
from imbue.mngr.utils import deps as deps_mod
from imbue.mngr.utils.deps import ALL_DEPS
from imbue.mngr.utils.deps import CORE_DEPS
from imbue.mngr.utils.deps import DependencyCategory
from imbue.mngr.utils.deps import InstallMethod
from imbue.mngr.utils.deps import OPTIONAL_DEPS
from imbue.mngr.utils.deps import OsName
from imbue.mngr.utils.deps import SystemDependency
from imbue.mngr.utils.deps import check_bash_version
from imbue.mngr.utils.deps import describe_install_commands
from imbue.mngr.utils.deps import detect_os
from imbue.mngr.utils.deps import install_dep
from imbue.mngr.utils.deps import install_deps_batch
from imbue.mngr.utils.deps import install_modern_bash

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


# -- describe_install_commands --


def test_describe_install_commands_empty_deps() -> None:
    """describe_install_commands returns empty list for no deps."""
    assert describe_install_commands([], OsName.MACOS) == []
    assert describe_install_commands([], OsName.LINUX) == []


def test_describe_install_commands_brew_only_on_macos() -> None:
    """Brew deps on macOS produce a single 'brew install' command."""
    deps = [
        SystemDependency(
            binary="tmux",
            purpose="test",
            macos_hint="brew install tmux",
            linux_hint="apt-get install tmux",
            install_method=InstallMethod(brew_package="tmux", apt_package="tmux"),
        ),
        SystemDependency(
            binary="jq",
            purpose="test",
            macos_hint="brew install jq",
            linux_hint="apt-get install jq",
            install_method=InstallMethod(brew_package="jq", apt_package="jq"),
        ),
    ]
    commands = describe_install_commands(deps, OsName.MACOS)
    assert commands == ["brew install tmux jq"]


def test_describe_install_commands_apt_only_on_linux() -> None:
    """Apt deps on Linux produce a single 'sudo apt-get install' command."""
    deps = [
        SystemDependency(
            binary="git",
            purpose="test",
            macos_hint="brew install git",
            linux_hint="apt-get install git",
            install_method=InstallMethod(brew_package="git", apt_package="git"),
        ),
    ]
    commands = describe_install_commands(deps, OsName.LINUX)
    assert commands == ["sudo apt-get install -y git"]


def test_describe_install_commands_custom_script() -> None:
    """Custom install scripts produce curl-pipe-bash commands."""
    deps = [
        SystemDependency(
            binary="claude",
            purpose="test",
            macos_hint="curl -fsSL https://example.com/install.sh | bash",
            linux_hint="curl -fsSL https://example.com/install.sh | bash",
            install_method=InstallMethod(custom_install_script="https://example.com/install.sh"),
        ),
    ]
    commands = describe_install_commands(deps, OsName.MACOS)
    assert commands == ["curl -fsSL https://example.com/install.sh | bash"]


def test_describe_install_commands_mixed_brew_and_custom_on_macos() -> None:
    """Brew commands appear before custom script commands."""
    deps = [
        SystemDependency(
            binary="tmux",
            purpose="test",
            macos_hint="brew install tmux",
            linux_hint="apt-get install tmux",
            install_method=InstallMethod(brew_package="tmux", apt_package="tmux"),
        ),
        SystemDependency(
            binary="claude",
            purpose="test",
            macos_hint="curl | bash",
            linux_hint="curl | bash",
            install_method=InstallMethod(custom_install_script="https://example.com/install.sh"),
        ),
    ]
    commands = describe_install_commands(deps, OsName.MACOS)
    assert len(commands) == 2
    assert commands[0] == "brew install tmux"
    assert commands[1] == "curl -fsSL https://example.com/install.sh | bash"


def test_describe_install_commands_skips_deps_without_install_method() -> None:
    """Deps with install_method=None are silently skipped."""
    deps = [
        SystemDependency(
            binary="ssh",
            purpose="test",
            macos_hint="included with macOS",
            linux_hint="apt-get install openssh-client",
            install_method=None,
        ),
    ]
    commands = describe_install_commands(deps, OsName.MACOS)
    assert commands == []


def test_describe_install_commands_fallback_manual_install() -> None:
    """Deps with install_method but no matching platform package get a manual install comment."""
    deps = [
        SystemDependency(
            binary="special-tool",
            purpose="test",
            macos_hint="install manually",
            linux_hint="install manually",
            install_method=InstallMethod(),
        ),
    ]
    commands = describe_install_commands(deps, OsName.MACOS)
    assert len(commands) == 1
    assert "special-tool" in commands[0]
    assert "install manually" in commands[0]


# -- install_dep --


def test_install_dep_returns_false_for_no_install_method() -> None:
    """install_dep returns False when install_method is None."""
    dep = SystemDependency(
        binary="no-method",
        purpose="test",
        macos_hint="n/a",
        linux_hint="n/a",
        install_method=None,
    )
    assert install_dep(dep, OsName.LINUX) is False


def test_install_dep_returns_false_for_empty_install_method() -> None:
    """install_dep returns False when install_method has no matching package for the OS."""
    dep = SystemDependency(
        binary="no-match",
        purpose="test",
        macos_hint="n/a",
        linux_hint="n/a",
        install_method=InstallMethod(),
    )
    assert install_dep(dep, OsName.LINUX) is False
    assert install_dep(dep, OsName.MACOS) is False


# -- install_deps_batch with no_auto_install deps --


def test_install_deps_batch_reports_no_auto_install_as_failed() -> None:
    """Deps with install_method but no matching platform method are reported as failed."""
    dep = SystemDependency(
        binary="no-match-xyz",
        purpose="test",
        macos_hint="n/a",
        linux_hint="n/a",
        install_method=InstallMethod(),
    )
    result = install_deps_batch([dep], OsName.MACOS)
    assert dep in result


# -- _install_via_brew / _install_via_apt / _install_via_script --


def test_install_functions_return_false_when_tools_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install functions return False when underlying package managers are absent.

    Exercises _install_via_brew, _install_via_apt, _install_via_script, install_dep,
    install_deps_batch, and install_modern_bash by making shutil.which return None.
    """
    monkeypatch.setattr(deps_mod.shutil, "which", lambda _name: None)

    # install_dep: custom_install_script without curl
    custom_dep = SystemDependency(
        binary="custom-tool",
        purpose="test",
        macos_hint="curl | bash",
        linux_hint="curl | bash",
        install_method=InstallMethod(custom_install_script="https://example.com/install.sh"),
    )
    assert install_dep(custom_dep, OsName.LINUX) is False

    # install_dep: brew_package on macOS without brew
    brew_dep = SystemDependency(
        binary="tmux",
        purpose="test",
        macos_hint="brew install tmux",
        linux_hint="apt-get install tmux",
        install_method=InstallMethod(brew_package="tmux"),
    )
    assert install_dep(brew_dep, OsName.MACOS) is False

    # install_dep: apt_package on Linux without apt-get
    apt_dep = SystemDependency(
        binary="tmux",
        purpose="test",
        macos_hint="brew install tmux",
        linux_hint="apt-get install tmux",
        install_method=InstallMethod(apt_package="tmux"),
    )
    assert install_dep(apt_dep, OsName.LINUX) is False

    # install_deps_batch: brew packages fail when brew is missing
    assert brew_dep in install_deps_batch([brew_dep], OsName.MACOS)

    # install_deps_batch: apt packages fail when apt-get is missing
    assert apt_dep in install_deps_batch([apt_dep], OsName.LINUX)

    # install_deps_batch: custom script deps fail when curl is missing
    assert custom_dep in install_deps_batch([custom_dep], OsName.LINUX)

    # install_modern_bash: delegates to _install_via_brew
    assert install_modern_bash() is False
