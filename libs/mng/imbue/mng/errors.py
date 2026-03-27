from pathlib import Path

from click import ClickException

from imbue.mng.primitives import AgentId
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ImageReference
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId


class BaseMngError(Exception):
    """Base exception for all mng errors."""


class MngError(ClickException, BaseMngError):
    """Base exception for all user-facing mng errors.

    All MngError subclasses can provide a user_help_text attribute that contains
    additional context to help the user understand and resolve the error.
    This help text is displayed by the CLI when the error is raised.
    """

    user_help_text: str | None = None

    def format_message(self) -> str:
        if self.user_help_text:
            return str(self) + "  [" + self.user_help_text + "]"
        return str(self)


class UserInputError(MngError):
    """Raised when user input is invalid."""

    user_help_text = "Check the command syntax with 'mng --help' or 'mng <command> --help'."


class ParseSpecError(MngError, ValueError):
    """Raised when parsing a specification string fails."""


class InvalidRelativePathError(MngError, ValueError):
    """Raised when a path that should be relative is actually absolute."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Path must be relative, got absolute path: {path}")


class HostError(BaseMngError):
    """Base class for host-related errors."""


class InvalidActivityTypeError(HostError, ValueError):
    """Raised when an invalid activity type is used."""


class HostConnectionError(HostError):
    """Raised when unable to connect to a host."""


class HostOfflineError(HostConnectionError):
    """Raised when unable to connect to a host because it is offline."""


class HostAuthenticationError(HostConnectionError):
    """Raised when unable to connect to a host because authentication failed."""


class HostDataSchemaError(HostError):
    """Raised when host data.json has an incompatible schema.

    This typically happens after mng is upgraded and the data format changed.
    """

    def __init__(self, data_path: str, validation_error: str) -> None:
        self.data_path = data_path
        self.validation_error = validation_error
        data_dir = str(Path(data_path).parent)
        message = (
            f"Host data file has incompatible schema: {data_path}\n"
            f"This usually means mng was upgraded and the data format changed.\n"
            f"To fix, either delete the file:\n"
            f"  rm {data_path}\n"
            f"Or run:\n"
            f'  claude --add-dir {data_dir} -p "migrate {data_path} to the new schema"'
        )
        super().__init__(message)
        self.user_help_text = f"Validation error details: {validation_error}"


class CommandTimeoutError(HostError):
    """Raised when a command execution times out."""


class LockNotHeldError(HostError):
    """Raised when attempting to use a lock that is not held."""


class AgentError(BaseMngError):
    """Base class for agent-related errors."""


class NoCommandDefinedError(AgentError, ValueError):
    """Raised when no command is defined for an agent type."""


class AgentNotFoundError(AgentError, MngError):
    """No agent with this ID exists."""

    user_help_text = "Use 'mng list' to see available agents."

    def __init__(self, agent_identifier: str) -> None:
        self.agent_identifier = agent_identifier
        super().__init__(f"Agent not found: {agent_identifier}")


class AgentNotFoundOnHostError(AgentError):
    """No agent with this ID exists on the specified host."""

    user_help_text = "Use 'mng list' to see all agents and their host assignments."

    def __init__(self, agent_id: AgentId, host_id: HostId) -> None:
        self.agent_id = agent_id
        self.host_id = host_id
        super().__init__(f"Agent {agent_id} not found on host {host_id}")


class SendMessageError(AgentError):
    """Failed to send a message to an agent."""

    def __init__(self, agent_name: str, reason: str) -> None:
        self.agent_name = agent_name
        self.reason = reason
        super().__init__(f"Failed to send message to agent {agent_name}: {reason}")


class AgentStartError(AgentError):
    """Failed to start an agent's tmux session."""

    def __init__(self, agent_name: str, reason: str) -> None:
        self.agent_name = agent_name
        self.reason = reason
        super().__init__(f"Failed to start agent {agent_name}: {reason}")


class ProviderError(MngError):
    """Base class for all provider-related errors."""


class ProviderInstanceNotFoundError(ProviderError):
    """No provider instance with this name exists."""

    user_help_text = (
        "Check your mng configuration for available providers.\nBuilt-in providers include 'local' and 'docker'."
    )

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        self.provider_name = provider_name
        super().__init__(f"Provider {provider_name} not found")


class ProviderNotAuthorizedError(ProviderError):
    """Provider instance is not authorized/authenticated."""

    def __init__(self, provider_name: ProviderInstanceName, auth_help: str | None = None) -> None:
        self.provider_name = provider_name
        message = f"Provider '{provider_name}' is not authorized."
        if auth_help:
            message = f"{message} {auth_help}"
        super().__init__(message)
        self.user_help_text = (
            f"To disable this provider, run:\n"
            f"  mng config set --scope user providers.{provider_name}.is_enabled false\n"
            f"Or disable the provider backend entirely by removing it from enabled_backends in your config."
        )


class HostNotFoundError(ProviderError):
    """No host with this ID or name exists."""

    user_help_text = "Use 'mng list' to see available hosts and agents."

    def __init__(self, host: HostId | HostName) -> None:
        self.host = host
        super().__init__(f"Host not found: {host}")


class HostCreationError(ProviderError):
    """Failed to create a host."""


class ImageNotFoundError(HostCreationError):
    """The specified image does not exist or is invalid."""

    def __init__(self, image: ImageReference) -> None:
        self.image = image
        super().__init__(f"Image not found: {image}")


class ResourceAllocationError(HostCreationError):
    """Failed to allocate resources for the host."""


class HostNameConflictError(ProviderError):
    """A host with this name already exists."""

    user_help_text = "Choose a different host name, or destroy the existing host first with 'mng destroy'."

    def __init__(self, name: HostName) -> None:
        self.name = name
        super().__init__(f"Host name already exists: {name}")


class HostNotRunningError(ProviderError):
    """Host is not in RUNNING state."""

    user_help_text = "Start the host first with 'mng start <host>'."

    def __init__(self, host_id: HostId, state: HostState) -> None:
        self.host_id = host_id
        self.state = state
        super().__init__(f"Host {host_id} is not running (state: {state})")


class HostNotStoppedError(ProviderError):
    """Host is not in STOPPED state."""

    user_help_text = "Stop the host first with 'mng stop <host>'."

    def __init__(self, host_id: HostId, state: HostState) -> None:
        self.host_id = host_id
        self.state = state
        super().__init__(f"Host {host_id} is not stopped (state: {state})")


class SnapshotError(ProviderError):
    """Base class for snapshot-related errors."""


class SnapshotNotFoundError(SnapshotError):
    """No snapshot with this ID exists."""

    user_help_text = "Use 'mng snapshot list <host>' to see available snapshots."

    def __init__(self, snapshot_id: SnapshotId) -> None:
        self.snapshot_id = snapshot_id
        super().__init__(f"Snapshot not found: {snapshot_id}")


class SnapshotsNotSupportedError(SnapshotError):
    """Provider does not support snapshots."""

    user_help_text = (
        "Snapshots are only available for cloud providers like Modal. The local provider does not support snapshots."
    )

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        self.provider_name = provider_name
        super().__init__(f"Provider {provider_name} does not support snapshots")


class TagLimitExceededError(ProviderError):
    """Tags exceed provider's storage limit."""

    def __init__(self, limit: int, actual: int) -> None:
        self.limit = limit
        self.actual = actual
        super().__init__(f"Tag limit exceeded: {actual} tags (limit: {limit})")


class LocalHostNotStoppableError(ProviderError):
    """Raised when attempting to stop the local host."""

    def __init__(self) -> None:
        super().__init__("Cannot stop the local host - it is your local computer")


class LocalHostNotDestroyableError(ProviderError):
    """Raised when attempting to destroy the local host."""

    def __init__(self) -> None:
        super().__init__("Cannot destroy the local host - it is your local computer")


class PluginSpecifierError(BaseMngError, ValueError):
    """Raised when a plugin specifier is invalid or cannot be resolved."""


class PluginMngError(MngError):
    """Raised when a plugin encounters an error during provisioning.

    Plugins should raise this error in the on_before_agent_provisioning hook
    when preconditions are not met (e.g., missing environment variables,
    missing required files).
    """


class ModalAuthError(PluginMngError):
    """Modal authentication failed due to missing or invalid token."""

    def __init__(self) -> None:
        super().__init__(
            "Modal authentication failed. Token missing or invalid. "
            "You can disable the modal plugin by running "
            "'mng config set --scope user plugins.modal.enabled false', "
            "or by passing --disable-plugin modal to individual commands. "
            "To configure modal credentials, see https://modal.com/docs/reference/modal.config"
        )


class ConfigError(MngError):
    """Base class for config errors."""


class ConfigNotFoundError(ConfigError):
    """Config file not found."""


class ConfigParseError(ConfigError):
    """Failed to parse config file."""


class ConfigKeyNotFoundError(ConfigError, KeyError):
    """Configuration key not found."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Key not found: {key}")


class ConfigStructureError(ConfigError, TypeError):
    """Invalid configuration structure."""


class UnknownBackendError(ConfigError):
    """Unknown provider backend."""


class NestedTmuxError(MngError):
    """Cannot attach to tmux session from inside another tmux session."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        super().__init__(
            f"You're already in a tmux session. You can attach to the agent with:\n  tmux attach -t {session_name}"
        )
        self.user_help_text = (
            "To allow mng to attach automatically inside tmux, run:\n"
            "  mng config set --scope user is_nested_tmux_allowed true"
        )


class BinaryNotInstalledError(MngError):
    """Raised when a required system binary is not installed."""

    def __init__(self, binary: str, purpose: str, install_hint: str) -> None:
        self.user_help_text = install_hint
        super().__init__(f"{binary} is required for {purpose} but was not found on PATH")
