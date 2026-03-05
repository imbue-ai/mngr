from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.mng import hookimpl
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng_claude_zygote.plugin import ClaudeZygoteAgent
from imbue.mng_claude_zygote.plugin import ClaudeZygoteConfig

_ECHO_MODEL_NAME = "echo"


class TestCoderConfig(ClaudeZygoteConfig):
    """Config for the test-coder agent type.

    Extends ClaudeZygoteConfig with defaults suitable for testing:
    - install_llm is True (needed for chat)
    - The command is overridden to a simple idle loop instead of Claude Code
    """

    install_llm_echo: bool = Field(
        default=True,
        description="Whether to install the llm-echo plugin during provisioning.",
    )


class TestCoderAgent(ClaudeZygoteAgent):
    """A test changeling agent that uses the echo model instead of real LLMs.

    Designed for end-to-end testing without API keys. The main agent
    process is a simple idle loop (no Claude Code), and the chat
    interface uses the llm echo model which returns predictable responses.
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
            'echo "Test agent running (echo model). Use mng chat to interact." && while true; do sleep 60; done'
        )

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Provision the test agent with the echo model.

        Runs the standard ClaudeZygoteAgent provisioning (which installs
        llm, creates event dirs, etc.) and then installs the llm-echo
        plugin so the echo model is available for chat.

        Also writes .changelings/settings.toml with model = "echo"
        so that chat.sh uses the echo model by default.
        """
        super().provision(host, options, mng_ctx)

        config = self._get_test_coder_config()

        if config.install_llm_echo:
            _install_llm_echo_plugin(host)

        _configure_echo_model_as_default(host, self.work_dir)

    def _get_test_coder_config(self) -> TestCoderConfig:
        """Get the test-coder-specific config."""
        if not isinstance(self.agent_config, TestCoderConfig):
            raise RuntimeError(
                f"TestCoderAgent requires TestCoderConfig, got {type(self.agent_config).__name__}. "
                "This indicates the agent type was registered with the wrong config class."
            )
        return self.agent_config


def _install_llm_echo_plugin(host: OnlineHostInterface) -> None:
    """Install the llm-echo plugin on the host.

    Tries `llm install llm-echo` which uses pip under the hood to install
    the plugin into llm's managed environment. This works when llm-echo
    is available via pip (either from PyPI or a local editable install
    visible to the host's Python environment).
    """
    logger.info("Installing llm-echo plugin")

    result = host.execute_command(
        "llm install llm-echo",
        timeout_seconds=120.0,
    )
    if not result.success:
        raise RuntimeError(f"Failed to install llm-echo: {result.stderr}")

    logger.info("llm-echo plugin installed successfully")


def _configure_echo_model_as_default(host: OnlineHostInterface, work_dir: Path) -> None:
    """Write .changelings/settings.toml with model = "echo".

    This ensures chat.sh uses the echo model by default, so no
    API keys are needed for the chat interface.
    """
    settings_dir = work_dir / ".changelings"
    settings_path = settings_dir / "settings.toml"

    settings_content = '[chat]\nmodel = "{}"\n'.format(_ECHO_MODEL_NAME)

    host.execute_command(
        f"mkdir -p {settings_dir}",
        timeout_seconds=10.0,
    )
    host.write_text_file(settings_path, settings_content)
    logger.info("Configured echo model as default chat model")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the test-coder agent type."""
    return ("test-coder", TestCoderAgent, TestCoderConfig)
