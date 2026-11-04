"""The state the smoke helpers reach through an interface, so a test can stand a local one in."""

from abc import ABC
from abc import abstractmethod
from pathlib import PurePosixPath

from imbue.imbue_common.mutable_model import MutableModel


class WorkspaceShellInterface(MutableModel, ABC):
    """Runs shell commands inside one workspace."""

    @abstractmethod
    def run(self, command: str, cwd: PurePosixPath, timeout_seconds: float) -> str:
        """Run ``command`` from ``cwd`` inside the workspace and return its stdout; raise SmokeWorkspaceExecError if it fails."""
