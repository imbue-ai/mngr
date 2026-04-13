import os
from collections.abc import Sequence

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import StringField

_SHELL_TIMEOUT_SECONDS = 30.0


class ShellCommandConfig(FrozenModel):
    """Configuration for a single shell command data source."""

    name: str = Field(description="Human-readable name")
    header: str = Field(description="Column header text")
    command: str = Field(description="Shell command to run per agent")


class ShellCommandDataSource(FrozenModel):
    """Runs user-defined shell commands per agent and produces StringField values.

    Each configured shell command becomes its own field. The shell command runs once
    per agent in parallel. Its stdout (trimmed) becomes the StringField value.
    Commands receive env vars from cached fields (MNGR_FIELD_<KEY>=<value>).
    """

    field_key: str = Field(description="Field key for this shell command's output")
    config: ShellCommandConfig = Field(description="Shell command configuration")
    timeout_seconds: float = Field(default=_SHELL_TIMEOUT_SECONDS, description="Per-process timeout in seconds")

    @property
    def name(self) -> str:
        return f"shell_{self.field_key}"

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def columns(self) -> dict[str, str]:
        return {self.field_key: self.config.header}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {self.field_key: StringField}

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        cg = mngr_ctx.concurrency_group
        errors: list[str] = []
        fields: dict[AgentName, dict[str, FieldValue]] = {}

        processes: list[tuple[AgentName, RunningProcess]] = []
        try:
            with cg.make_concurrency_group(
                name=f"shell-{self.field_key}",
                exit_timeout_seconds=self.timeout_seconds,
            ) as child_cg:
                for agent in agents:
                    env = _build_shell_env(agent, cached_fields.get(agent.name, {}))
                    proc = child_cg.run_process_in_background(
                        ["sh", "-c", self.config.command],
                        timeout=self.timeout_seconds,
                        is_checked_by_group=False,
                        env=env,
                    )
                    processes.append((agent.name, proc))
        except ConcurrencyExceptionGroup as exc:
            n_failed = len(exc.exceptions)
            errors.append(f"Shell '{self.config.name}': {n_failed} process(es) timed out or failed")
            logger.debug("Shell '{}' concurrency group error: {}", self.config.name, exc)

        for agent_name, proc in processes:
            rc = proc.returncode
            if rc == 0:
                stdout = proc.read_stdout().strip()
                if stdout:
                    fields[agent_name] = {self.field_key: StringField(value=stdout)}
            else:
                stderr = proc.read_stderr().strip()
                msg = f"Shell '{self.config.name}' failed for {agent_name} (exit {rc})"
                if stderr:
                    msg = f"{msg}: {stderr}"
                errors.append(msg)

        return fields, errors


def _build_shell_env(
    agent: AgentDetails,
    agent_cached: dict[str, FieldValue],
) -> dict[str, str]:
    """Build environment variables for a shell command.

    Includes standard MNGR_ vars plus MNGR_FIELD_<KEY> for each cached field.
    """
    env: dict[str, str] = {
        **os.environ,
        "MNGR_AGENT_NAME": str(agent.name),
        "MNGR_AGENT_BRANCH": agent.initial_branch or "",
        "MNGR_AGENT_STATE": str(agent.state),
        "MNGR_AGENT_PROVIDER": str(agent.host.provider_name),
    }

    # Add cached field values as env vars
    for key, field_value in agent_cached.items():
        env_key = f"MNGR_FIELD_{key.upper()}"
        if isinstance(field_value, PrField):
            env["MNGR_FIELD_PR_NUMBER"] = str(field_value.number)
            env["MNGR_FIELD_PR_URL"] = field_value.url
            env["MNGR_FIELD_PR_STATE"] = str(field_value.state)
        elif isinstance(field_value, CiField):
            env["MNGR_FIELD_CI_STATUS"] = str(field_value.status)
        elif isinstance(field_value, StringField):
            env[env_key] = field_value.value
        else:
            cell = field_value.display()
            env[env_key] = cell.text

    return env
