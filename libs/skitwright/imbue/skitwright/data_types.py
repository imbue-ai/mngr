from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel


class CommandResult(FrozenModel):
    """Result of executing a shell command."""

    command: str = Field(description="The shell command that was executed")
    exit_code: int = Field(description="Exit code of the command")
    stdout: str = Field(description="Captured standard output")
    stderr: str = Field(description="Captured standard error")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> bool:
        return self.exit_code != 0
