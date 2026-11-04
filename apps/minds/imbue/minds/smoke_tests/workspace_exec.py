"""Commands inside the journey's workspace, run the way the app runs them: through its own mngr.

The app reaches an Imbue Cloud workspace with ``mngr exec`` on the sign-in it
holds under its data home. The suite runs that same mngr with the app's host
dir and prefix, from the app's launch directory and on the environment the app
itself is given (:func:`app_environment`), so a command reaches the workspace
the way one of the app's own does.
"""

import os
import time
from collections.abc import Mapping
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.mngr_command import extract_exec_failure_detail
from imbue.minds.desktop_client.mngr_command import extract_exec_stdout
from imbue.minds.smoke_tests.binary import app_environment
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.interfaces import WorkspaceShellInterface
from imbue.minds.smoke_tests.primitives import SmokeWorkspaceExecError

# mngr's own start-up, host lookup and connection, on top of the command's timeout.
_MNGR_EXEC_OVERHEAD_SECONDS: Final[float] = 120.0
_SLOW_EXEC_WARNING_SECONDS: Final[float] = 60.0
_FAILURE_DETAIL_CHARACTER_COUNT: Final[int] = 2000


@pure
def workspace_exec_address(agent_id: str, host_id: str | None, provider_name: str | None) -> str:
    """The workspace's mngr address: ``AGENT@HOST.PROVIDER`` when its host is known, else its bare id.

    The full address has mngr read only that host; the bare id has it discover every provider's hosts.
    """
    if host_id and provider_name:
        return f"{agent_id}@{host_id}.{provider_name}"
    return agent_id


@pure
def workspace_exec_argv(
    mngr_executable: Path, workspace_address: str, command: str, cwd: PurePosixPath, timeout_seconds: float
) -> list[str]:
    return [
        str(mngr_executable),
        "exec",
        workspace_address,
        command,
        "--cwd",
        str(cwd),
        "--no-start",
        "--timeout",
        str(timeout_seconds),
        "--format",
        "json",
    ]


@pure
def app_mngr_environment(environ: Mapping[str, str], mngr_host_dir: Path, mngr_prefix: str) -> dict[str, str]:
    """The environment the app runs mngr on: the app's own, with its host dir and prefix."""
    return app_environment(environ, {"MNGR_HOST_DIR": str(mngr_host_dir), "MNGR_PREFIX": mngr_prefix})


class AppMngrWorkspaceShell(WorkspaceShellInterface):
    """Runs commands in one workspace through the launched app's own mngr."""

    mngr_executable: Path = Field(frozen=True, description="The app's own mngr.")
    launch_cwd: Path = Field(frozen=True, description="The directory the app was started from, outside any checkout.")
    mngr_host_dir: Path = Field(frozen=True, description="The ``MNGR_HOST_DIR`` the app runs mngr with.")
    mngr_prefix: NonEmptyStr = Field(frozen=True, description="The ``MNGR_PREFIX`` the app runs mngr with.")
    workspace_address: NonEmptyStr = Field(frozen=True, description="The workspace's mngr address.")

    def run(self, command: str, cwd: PurePosixPath, timeout_seconds: float) -> str:
        if not self.mngr_executable.is_file():
            raise SmokeWorkspaceExecError(
                f"The app's mngr is not at {self.mngr_executable}, so no command can reach its workspace."
            )
        started_at = time.monotonic()
        with ConcurrencyGroup(name="smoke-workspace-exec") as concurrency_group:
            finished = concurrency_group.run_process_to_completion(
                command=workspace_exec_argv(
                    self.mngr_executable, str(self.workspace_address), command, cwd, timeout_seconds
                ),
                timeout=timeout_seconds + _MNGR_EXEC_OVERHEAD_SECONDS,
                is_checked_after=False,
                cwd=self.launch_cwd,
                env=app_mngr_environment(os.environ, self.mngr_host_dir, str(self.mngr_prefix)),
            )
        elapsed_seconds = time.monotonic() - started_at
        if finished.is_timed_out:
            raise SmokeWorkspaceExecError(
                f"mngr exec in {self.workspace_address} did not return within "
                f"{timeout_seconds + _MNGR_EXEC_OVERHEAD_SECONDS:.0f}s."
            )
        stdout = extract_exec_stdout(finished.stdout)
        if finished.returncode != 0 or stdout is None:
            detail = extract_exec_failure_detail(finished.stdout) or finished.stderr.strip()
            raise SmokeWorkspaceExecError(
                f"A command failed in workspace {self.workspace_address} (mngr exited {finished.returncode}): "
                f"{detail[-_FAILURE_DETAIL_CHARACTER_COUNT:]}"
            )
        if elapsed_seconds > _SLOW_EXEC_WARNING_SECONDS:
            logger.warning("mngr exec in {} took {:.0f}s", self.workspace_address, elapsed_seconds)
        return stdout


def app_mngr_workspace_shell(session: StudioAppSession, workspace_address: str) -> AppMngrWorkspaceShell:
    """A shell into ``workspace_address`` through the mngr ``session``'s app runs."""
    return AppMngrWorkspaceShell(
        mngr_executable=session.mngr_executable,
        launch_cwd=session.launch_cwd,
        mngr_host_dir=session.mngr_host_dir,
        mngr_prefix=session.mngr_prefix,
        workspace_address=NonEmptyStr(workspace_address),
    )
