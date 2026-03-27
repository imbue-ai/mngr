import platform
import shutil

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import BinaryNotInstalledError


class SystemDependency(FrozenModel):
    """A system binary that mngr requires at runtime."""

    binary: str = Field(description="Name of the binary on PATH")
    purpose: str = Field(description="What this binary is used for")
    macos_hint: str = Field(description="Installation instructions for macOS")
    linux_hint: str = Field(description="Installation instructions for Linux")

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


RSYNC = SystemDependency(
    binary="rsync",
    purpose="file sync",
    macos_hint="brew install rsync",
    linux_hint="sudo apt-get install rsync",
)

TMUX = SystemDependency(
    binary="tmux",
    purpose="agent session management",
    macos_hint="brew install tmux",
    linux_hint="sudo apt-get install tmux",
)

GIT = SystemDependency(
    binary="git",
    purpose="source control",
    macos_hint="brew install git",
    linux_hint="sudo apt-get install git",
)

JQ = SystemDependency(
    binary="jq",
    purpose="JSON processing",
    macos_hint="brew install jq",
    linux_hint="sudo apt-get install jq",
)
