import platform
import shutil
import subprocess
from collections.abc import Sequence
from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.errors import MngrError


class OsName(UpperCaseStrEnum):
    """Supported operating system names."""

    MACOS = auto()
    LINUX = auto()


class DependencyCategory(UpperCaseStrEnum):
    """Whether a system dependency is required or optional."""

    CORE = auto()
    OPTIONAL = auto()


class InstallMethod(FrozenModel):
    """How to install a system dependency on each platform."""

    brew_package: str | None = Field(default=None, description="Homebrew package name (macOS)")
    apt_package: str | None = Field(default=None, description="apt package name (Linux)")
    custom_install_script: str | None = Field(default=None, description="URL for curl-pipe-bash installer")


class SystemDependency(FrozenModel):
    """A system binary that mngr requires at runtime."""

    binary: str = Field(description="Name of the binary on PATH")
    purpose: str = Field(description="What this binary is used for")
    macos_hint: str = Field(description="Installation instructions for macOS")
    linux_hint: str = Field(description="Installation instructions for Linux")
    category: DependencyCategory = Field(
        default=DependencyCategory.CORE, description="Whether this dependency is core or optional"
    )
    install_method: InstallMethod | None = Field(
        default=None, description="How to install this dependency programmatically"
    )

    @property
    def install_hint(self) -> str:
        """Return the installation hint for the current platform."""
        if platform.system() == "Darwin":
            return self.macos_hint
        return self.linux_hint

    def is_available(self) -> bool:
        """Check if this binary is available on PATH."""
        return shutil.which(self.binary) is not None

    def require(self) -> None:
        """Raise BinaryNotInstalledError if this binary is not available."""
        if not self.is_available():
            raise BinaryNotInstalledError(self.binary, self.purpose, self.install_hint)


SSH = SystemDependency(
    binary="ssh",
    purpose="remote host connections and git over SSH",
    macos_hint="ssh is included with macOS",
    linux_hint="sudo apt-get install openssh-client",
    category=DependencyCategory.CORE,
    install_method=InstallMethod(apt_package="openssh-client"),
)

GIT = SystemDependency(
    binary="git",
    purpose="source control",
    macos_hint="brew install git",
    linux_hint="sudo apt-get install git",
    category=DependencyCategory.CORE,
    install_method=InstallMethod(brew_package="git", apt_package="git"),
)

TMUX = SystemDependency(
    binary="tmux",
    purpose="agent session management",
    macos_hint="brew install tmux",
    linux_hint="sudo apt-get install tmux",
    category=DependencyCategory.CORE,
    install_method=InstallMethod(brew_package="tmux", apt_package="tmux"),
)

JQ = SystemDependency(
    binary="jq",
    purpose="JSON processing",
    macos_hint="brew install jq",
    linux_hint="sudo apt-get install jq",
    category=DependencyCategory.CORE,
    install_method=InstallMethod(brew_package="jq", apt_package="jq"),
)

RSYNC = SystemDependency(
    binary="rsync",
    purpose="file sync",
    macos_hint="brew install rsync",
    linux_hint="sudo apt-get install rsync",
    category=DependencyCategory.OPTIONAL,
    install_method=InstallMethod(brew_package="rsync", apt_package="rsync"),
)

UNISON = SystemDependency(
    binary="unison",
    purpose="pair operations (continuous file sync)",
    macos_hint="brew install unison",
    linux_hint="sudo apt-get install unison",
    category=DependencyCategory.OPTIONAL,
    install_method=InstallMethod(brew_package="unison", apt_package="unison"),
)

CLAUDE = SystemDependency(
    binary="claude",
    purpose="Claude agent type",
    macos_hint="curl -fsSL https://claude.ai/install.sh | bash",
    linux_hint="curl -fsSL https://claude.ai/install.sh | bash",
    category=DependencyCategory.OPTIONAL,
    install_method=InstallMethod(custom_install_script="https://claude.ai/install.sh"),
)

CORE_DEPS: tuple[SystemDependency, ...] = (SSH, GIT, TMUX, JQ)
OPTIONAL_DEPS: tuple[SystemDependency, ...] = (CLAUDE, RSYNC, UNISON)
ALL_DEPS: tuple[SystemDependency, ...] = CORE_DEPS + OPTIONAL_DEPS


def detect_os() -> OsName:
    """Detect the current operating system."""
    system = platform.system()
    if system == "Darwin":
        return OsName.MACOS
    if system == "Linux":
        return OsName.LINUX
    raise MngrError(f"Unsupported operating system: {system}. mngr supports macOS and Linux.")


def check_bash_version(minimum: int = 4) -> bool:
    """Check if the PATH-resolved bash is at least the given major version.

    Returns True if bash >= minimum, False otherwise.
    Only practically relevant on macOS where /bin/bash is version 3.2.
    """
    try:
        result = subprocess.run(
            ["bash", "-c", "echo ${BASH_VERSINFO[0]}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        version = int(result.stdout.strip())
        return version >= minimum
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def install_modern_bash() -> bool:
    """Install modern bash (4+) via Homebrew on macOS. Returns True on success."""
    return _install_via_brew(["bash"])


def install_dep(dep: SystemDependency, os_name: OsName) -> bool:
    """Install a single system dependency. Returns True on success."""
    if dep.install_method is None:
        return False

    if dep.install_method.custom_install_script is not None:
        return _install_via_script(dep.install_method.custom_install_script)

    if os_name == OsName.MACOS and dep.install_method.brew_package is not None:
        return _install_via_brew([dep.install_method.brew_package])

    if os_name == OsName.LINUX and dep.install_method.apt_package is not None:
        return _install_via_apt([dep.install_method.apt_package])

    return False


def install_deps_batch(deps: Sequence[SystemDependency], os_name: OsName) -> list[SystemDependency]:
    """Install multiple dependencies, batching brew/apt calls. Returns list of deps that failed."""
    # Separate deps by install mechanism
    brew_packages: list[str] = []
    apt_packages: list[str] = []
    custom_deps: list[SystemDependency] = []
    brew_dep_map: dict[str, SystemDependency] = {}
    apt_dep_map: dict[str, SystemDependency] = {}

    for dep in deps:
        if dep.install_method is None:
            continue
        if dep.install_method.custom_install_script is not None:
            custom_deps.append(dep)
        elif os_name == OsName.MACOS and dep.install_method.brew_package is not None:
            brew_packages.append(dep.install_method.brew_package)
            brew_dep_map[dep.install_method.brew_package] = dep
        elif os_name == OsName.LINUX and dep.install_method.apt_package is not None:
            apt_packages.append(dep.install_method.apt_package)
            apt_dep_map[dep.install_method.apt_package] = dep
        else:
            pass

    failed: list[SystemDependency] = []

    # Batch install brew/apt packages
    if brew_packages:
        if not _install_via_brew(brew_packages):
            failed.extend(brew_dep_map[pkg] for pkg in brew_packages)

    if apt_packages:
        if not _install_via_apt(apt_packages):
            failed.extend(apt_dep_map[pkg] for pkg in apt_packages)

    # Custom installs one at a time
    for dep in custom_deps:
        if not install_dep(dep, os_name):
            failed.append(dep)

    return failed


def _install_via_brew(packages: list[str]) -> bool:
    """Install packages via Homebrew. Returns True on success."""
    if shutil.which("brew") is None:
        return False
    try:
        result = subprocess.run(
            ["brew", "install", *packages],
            timeout=300,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _install_via_apt(packages: list[str]) -> bool:
    """Install packages via apt-get. Returns True on success."""
    if shutil.which("apt-get") is None:
        return False
    try:
        subprocess.run(
            ["sudo", "apt-get", "update", "-qq"],
            timeout=60,
        )
        result = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "-qq", *packages],
            timeout=300,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _install_via_script(url: str) -> bool:
    """Install via curl-pipe-bash. Returns True on success."""
    if shutil.which("curl") is None:
        return False
    try:
        result = subprocess.run(
            ["bash", "-c", f"curl -fsSL {url} | bash"],
            timeout=120,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
