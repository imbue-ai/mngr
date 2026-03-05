from __future__ import annotations

import shlex
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.mng import hookimpl
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng_claude_changeling.plugin import ClaudeChangelingAgent
from imbue.mng_claude_changeling.plugin import ClaudeChangelingConfig

_MODEL_NAME = "matched-responses"

_LLM_MATCHED_RESPONSES_LOCAL_CHECKOUT = Path.home() / "project" / "llm-matched-responses"


class TestCoderProvisioningError(MngError, RuntimeError):
    """Raised when test-coder agent provisioning fails."""

    ...


class TestCoderConfig(ClaudeChangelingConfig):
    """Config for the test-coder agent type.

    Extends ClaudeChangelingConfig with defaults suitable for testing:
    - install_llm is True (needed for chat)
    - The command is overridden to a simple idle loop instead of Claude Code
    """

    install_llm_matched_responses: bool = Field(
        default=True,
        description="Whether to install the llm-matched-responses plugin during provisioning.",
    )


class TestCoderAgent(ClaudeChangelingAgent):
    """A test changeling agent that uses the matched-responses model instead of real LLMs.

    Designed for end-to-end testing without API keys. The main agent
    process is a simple idle loop (no Claude Code), and the chat
    interface uses the llm matched-responses model which returns
    predictable responses.
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Return a simple idle loop command instead of launching Claude Code.

        The test agent does not need Claude Code running -- it only needs
        to be a live process so mng considers it running. The chat
        interface (via llm live-chat) works independently.
        """
        return CommandString(
            'echo "Test agent running (matched-responses model). Use mng chat to interact." && '
            "while true; do sleep 60; done"
        )

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Provision the test agent with the matched-responses model.

        Runs the standard ClaudeChangelingAgent provisioning (which installs
        llm, creates event dirs, etc.) and then installs the
        llm-matched-responses plugin so the model is available for chat.

        Also writes .changelings/settings.toml with model = "matched-responses"
        so that chat.sh uses it by default.
        """
        super().provision(host, options, mng_ctx)

        config = self._get_test_coder_config()

        if config.install_llm_matched_responses:
            _install_llm_matched_responses_plugin(host)

        _configure_model_as_default(host, self.work_dir)

    def _get_test_coder_config(self) -> TestCoderConfig:
        """Get the test-coder-specific config."""
        if not isinstance(self.agent_config, TestCoderConfig):
            raise TestCoderProvisioningError(
                f"TestCoderAgent requires TestCoderConfig, got {type(self.agent_config).__name__}. "
                "This indicates the agent type was registered with the wrong config class."
            )
        return self.agent_config


def _install_llm_matched_responses_plugin(host: OnlineHostInterface) -> None:
    """Install the llm-matched-responses plugin on the host.

    Tries PyPI first (`llm install llm-matched-responses`). If that fails
    (e.g. not yet published), falls back to an editable install from the
    local checkout at ~/project/llm-matched-responses/.
    """
    logger.info("Installing llm-matched-responses plugin")

    result = host.execute_command(
        "llm install llm-matched-responses",
        timeout_seconds=120.0,
    )
    if result.success:
        logger.info("Installed llm-matched-responses from PyPI")
        return

    logger.debug("PyPI install failed, trying local checkout: {}", result.stderr[:200])

    local_checkout = _LLM_MATCHED_RESPONSES_LOCAL_CHECKOUT
    if not local_checkout.exists():
        raise TestCoderProvisioningError(
            f"llm-matched-responses is not available on PyPI and local checkout not found at {local_checkout}. "
            "Either publish the package or clone https://github.com/imbue-ai/llm-matched-responses "
            f"to {local_checkout}."
        )

    result = host.execute_command(
        f"llm install -e {shlex.quote(str(local_checkout))}",
        timeout_seconds=120.0,
    )
    if not result.success:
        raise TestCoderProvisioningError(f"Failed to install llm-matched-responses: {result.stderr}")

    logger.info("Installed llm-matched-responses from local checkout")


def _configure_model_as_default(host: OnlineHostInterface, work_dir: Path) -> None:
    """Write .changelings/settings.toml with model = "matched-responses".

    This ensures chat.sh uses the matched-responses model by default, so no
    API keys are needed for the chat interface.
    """
    settings_dir = work_dir / ".changelings"
    settings_path = settings_dir / "settings.toml"

    settings_content = '[chat]\nmodel = "{}"\n'.format(_MODEL_NAME)

    mkdir_result = host.execute_command(
        f"mkdir -p {settings_dir}",
        timeout_seconds=10.0,
    )
    if not mkdir_result.success:
        raise TestCoderProvisioningError(f"Failed to create settings directory {settings_dir}: {mkdir_result.stderr}")
    host.write_text_file(settings_path, settings_content)
    logger.info("Configured matched-responses model as default chat model")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the test-coder agent type."""
    return ("test-coder", TestCoderAgent, TestCoderConfig)
