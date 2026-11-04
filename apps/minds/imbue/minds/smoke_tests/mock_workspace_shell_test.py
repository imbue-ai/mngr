import subprocess
from pathlib import Path
from pathlib import PurePosixPath

from pydantic import Field

from imbue.minds.smoke_tests.interfaces import WorkspaceShellInterface
from imbue.minds.smoke_tests.primitives import SmokeWorkspaceExecError


class LocalWorkspaceShell(WorkspaceShellInterface):
    """A "workspace" that is this machine: each command runs under bash here, on a fixed environment."""

    environment: dict[str, str] = Field(frozen=True, description="The whole environment every command runs on")
    commands_run: list[str] = Field(default_factory=list, description="Every command run, in order")

    def run(self, command: str, cwd: PurePosixPath, timeout_seconds: float) -> str:
        self.commands_run.append(command)
        finished = subprocess.run(
            ["bash", "-c", command],
            cwd=Path(cwd),
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if finished.returncode != 0:
            raise SmokeWorkspaceExecError(f"`{command[:200]}` exited {finished.returncode}: {finished.stderr[-2000:]}")
        return finished.stdout
