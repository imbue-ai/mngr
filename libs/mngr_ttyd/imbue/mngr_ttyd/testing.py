"""Test utilities for mngr_ttyd: a thin subclass of the shared FakeHost."""

from collections.abc import Mapping
from pathlib import Path

from pydantic import Field

from imbue.mngr.api.testing import FakeHost as BaseFakeHost
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr_ttyd.plugin import TTYD_INSTALL_COMMAND


class FakeTtydHost(BaseFakeHost):
    """OnlineHostInterface fake for mngr_ttyd provisioning tests.

    Inherits the shared FakeHost, which executes commands on the local
    filesystem and writes real files, so tests can assert on real effects
    (directories that actually get created, scripts that actually land on disk
    with the right permissions) rather than on recorded command strings.

    Two operations are intercepted to keep tests deterministic and offline:
    - the ``command -v ttyd`` presence probe, whose result is driven by
      ``is_ttyd_installed`` (real execution would reflect whatever happens to
      be installed on the CI machine),
    - the ``TTYD_INSTALL_COMMAND`` GitHub download, which returns success
      without running ``curl`` (real execution would hit the network).

    Every command passed to ``execute_idempotent_command`` is recorded in
    ``executed_commands`` so tests can assert which provisioning steps ran.
    """

    is_ttyd_installed: bool = Field(default=True, description="Result the ttyd presence probe should report")
    executed_commands: list[str] = Field(
        default_factory=list, description="Commands passed to execute_idempotent_command, in order"
    )
    written_file_paths: list[Path] = Field(default_factory=list, description="Paths written via write_file, in order")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.executed_commands.append(command)
        if "command -v ttyd" in command:
            return CommandResult(stdout="", stderr="", success=self.is_ttyd_installed)
        if command == TTYD_INSTALL_COMMAND:
            return CommandResult(stdout="", stderr="", success=True)
        return super().execute_idempotent_command(
            command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds
        )

    def write_file(self, path: Path, content: bytes, mode: str | None = None) -> None:
        self.written_file_paths.append(path)
        super().write_file(path, content, mode)
        if mode is not None:
            path.chmod(int(mode, 8))
