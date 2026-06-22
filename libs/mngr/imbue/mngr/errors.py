from pathlib import Path
from typing import Any
from typing import Final
from typing import IO

from click import ClickException
from click import get_text_stream

from imbue.mngr.colors import ERROR_COLOR
from imbue.mngr.colors import RESET_COLOR
from imbue.mngr.colors import should_use_color
from imbue.mngr.plugin_catalog import get_plugin_install_hint
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import PluginKind
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId


class MngrError(ClickException):
    """Base exception for all user-facing mngr errors.

    All MngrError subclasses can provide a user_help_text attribute that contains
    additional context to help the user understand and resolve the error.
    This help text is displayed by the CLI when the error is raised.
    """

    user_help_text: str | None = None

    def format_message(self) -> str:
        if self.user_help_text:
            return str(self) + "  [" + self.user_help_text + "]"
        return str(self)

    def show(self, file: IO[Any] | None = None) -> None:
        """Render the error with a bold-red ``Error:`` prefix on a color-capable terminal.

        Gated on ``should_use_color`` so piped or ``NO_COLOR`` output stays plain,
        mirroring the colored ``ERROR:`` prefix that ``logger.error`` already uses.
        """
        if file is None:
            file = get_text_stream("stderr")
        message = f"Error: {self.format_message()}"
        if should_use_color(file):
            message = f"{ERROR_COLOR}{message}{RESET_COLOR}"
        # Write straight to the stream (the PREVENT_CLICK_ECHO ratchet forbids the
        # click helper here); this matches how the loguru stderr sink writes.
        file.write(message + "\n")
        file.flush()


class UserInputError(MngrError):
    """Raised when user input is invalid."""

    user_help_text = "Check the command syntax with 'mngr --help' or 'mngr <command> --help'."


class ParseSpecError(MngrError, ValueError):
    """Raised when parsing a specification string fails."""


class MismatchedPreselectionError(MngrError, ValueError):
    """Raised when a picker's preselected mask length does not match its options."""


class InvalidRelativePathError(MngrError, ValueError):
    """Raised when a path that should be relative is actually absolute."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Path must be relative, got absolute path: {path}")


class HostError(MngrError):
    """Base class for host-related errors.

    As a MngrError subclass, host errors are ClickException instances: when they
    reach the CLI they render as a clean ``Error: ...`` message (plus any
    user_help_text) instead of a traceback, and ``except MngrError`` handlers
    treat them as the user-facing errors they are.
    """


class InvalidActivityTypeError(HostError, ValueError):
    """Raised when an invalid activity type is used."""


class HostConnectionError(HostError):
    """Raised when unable to connect to a host."""


class HostOfflineError(HostConnectionError):
    """Raised when unable to connect to a host because it is offline."""


class HostAuthenticationError(HostConnectionError):
    """Raised when unable to connect to a host because authentication failed."""


class CorruptedAgentDataError(HostError):
    """Raised when an agent's data.json is unreadable after retries.

    FIXME: this should result in a new CORRUPTED agent state so the UI can
    surface the problem instead of silently dropping the agent from listings.
    """

    def __init__(self, agent_id: object, data_path: Path, parse_error: Exception) -> None:
        super().__init__("Agent {} has corrupted data at {}: {}".format(agent_id, data_path, parse_error))


class HostDataSchemaError(HostError):
    """Raised when host data.json has an incompatible schema.

    This typically happens after mngr is upgraded and the data format changed.
    """

    def __init__(self, data_path: str, validation_error: str) -> None:
        self.data_path = data_path
        self.validation_error = validation_error
        data_dir = str(Path(data_path).parent)
        message = (
            f"Host data file has incompatible schema: {data_path}\n"
            f"This usually means mngr was upgraded and the data format changed.\n"
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


class AgentError(MngrError):
    """Base class for agent-related errors.

    As a MngrError subclass, agent errors are ClickException instances: when they
    reach the CLI they render as a clean ``Error: ...`` message (plus any
    user_help_text) instead of a traceback, and ``except MngrError`` handlers
    treat them as the user-facing errors they are.
    """


class AgentInstallationError(AgentError):
    """Raised when an agent's CLI binary is missing and cannot be installed."""


class NoCommandDefinedError(AgentError, ValueError):
    """Raised when no command is defined for an agent type."""


class AgentNotFoundError(AgentError):
    """No agent with this ID exists."""

    user_help_text = "Use 'mngr list' to see available agents."

    def __init__(self, agent_identifier: str) -> None:
        self.agent_identifier = agent_identifier
        super().__init__(f"Agent not found: {agent_identifier}")


class AgentNotFoundOnHostError(AgentError):
    """No agent with this ID exists on the specified host."""

    user_help_text = "Use 'mngr list' to see all agents and their host assignments."

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


class DuplicateAgentNameError(AgentError):
    """An agent with this name already exists on the host."""

    user_help_text = (
        "Choose a different name. For 'mngr create', you can also use --reuse to reuse the existing agent."
    )

    def __init__(self, agent_name: AgentName, existing_agent_id: AgentId) -> None:
        self.agent_name = agent_name
        self.existing_agent_id = existing_agent_id
        super().__init__(f"An agent named '{agent_name}' already exists on this host (ID: {existing_agent_id})")


class AgentStateInconsistencyError(AgentError, RuntimeError):
    """Raised when an agent found during discovery is no longer present on the live host."""


class AgentStartError(AgentError):
    """Failed to start an agent's tmux session."""

    def __init__(self, agent_name: str, reason: str) -> None:
        self.agent_name = agent_name
        self.reason = reason
        super().__init__(f"Failed to start agent {agent_name}: {reason}")


class ProviderError(MngrError):
    """Base class for all provider-related errors."""

    provider_name: ProviderInstanceName

    def __init__(self, provider_name: ProviderInstanceName, message: str) -> None:
        self.provider_name = provider_name
        super().__init__(message)


class ProviderUnavailableError(ProviderError):
    """Provider backend is not reachable (e.g. Docker daemon not running).

    Commands that query multiple providers catch this and continue with
    the providers that *are* available, so a single offline backend does
    not block the entire operation.

    Carries structured fields so callers can render a consistent one-line
    summary (``short_reason`` + ``short_remediation``) without re-parsing the
    full message, while ``user_help_text`` keeps the verbose guidance.
    """

    # A concise phrase describing why the provider is unavailable (e.g. "AWS
    # credentials not configured"). Distinct from the verbose ``user_help_text``.
    short_reason: str
    # A concise next step the user can take (e.g. "run `aws configure`"), or None.
    short_remediation: str | None

    def __init__(
        self,
        provider_name: ProviderInstanceName,
        reason: str,
        user_help_text: str | None = None,
        short_remediation: str | None = None,
        # An explicit concise, single-line reason for the rendered summary. Defaults to
        # ``reason``; pass it when ``reason`` is long or multi-line (e.g. a cloud SDK
        # message) so the one-line-per-provider listing stays glanceable.
        short_reason: str | None = None,
    ) -> None:
        self.short_reason = short_reason or reason
        self.short_remediation = short_remediation
        super().__init__(
            provider_name,
            f"Provider '{provider_name}' is not available: {reason}. "
            f"Any agents managed by this provider could not be reached.",
        )
        # Providers whose "unavailable" cause is not a local daemon (e.g. a cloud
        # provider failing on credentials/subscription) pass curated guidance so
        # the user is not told to "start Docker" for an auth problem.
        self.user_help_text = user_help_text or (
            f"Ensure the provider backend is running (e.g. start Docker), or disable the provider:\n"
            f"  mngr config set --scope user providers.{provider_name}.is_enabled false"
        )


class ProviderEmptyError(ProviderError):
    """Provider was reached and definitively reports that nothing has been
    created yet (e.g. the Modal per-user environment does not exist).

    Distinct from ``ProviderUnavailableError``: there, the backend's state is
    *unknown* (we couldn't reach it, agents may still exist), so silently
    skipping risks hiding real data. Here, the backend's state is *known to
    be empty*, so read paths (``mngr list`` / ``mngr gc`` / discovery) can
    always safely skip this provider -- the resulting listing is correct
    (zero of zero), not misleading.
    """

    def __init__(self, provider_name: ProviderInstanceName, reason: str) -> None:
        super().__init__(provider_name, f"Provider '{provider_name}' has no state yet: {reason}")


class ProviderDiscoveryError(ProviderError):
    """Wraps an exception raised inside a single provider's discovery so
    callers can attribute the failure to the offending provider instance.

    The wrapped exception is preserved in ``__cause__``; ``provider_name``
    carries the ``ProviderInstanceName`` of the failing instance so error
    handlers (e.g. minds' providers panel surfacing per-provider error
    badges) don't have to pattern-match the message string.
    """

    def __init__(self, provider_name: ProviderInstanceName, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(provider_name, f"Discovery failed for provider '{provider_name}': {cause}")


class ProviderInstanceNotFoundError(ProviderError):
    """No provider instance with this name exists."""

    user_help_text = (
        "Check your mngr configuration for available providers.\nBuilt-in providers include 'local' and 'docker'."
    )

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(provider_name, f"Provider {provider_name} not found")


class ProviderNotAuthorizedError(ProviderUnavailableError):
    """Provider instance is not authenticated/authorized (missing or invalid credentials).

    A specialization of ``ProviderUnavailableError``: the backend may be reachable
    in principle, but without valid credentials its state is unknown, so read paths
    (``mngr list`` / ``mngr gc`` / discovery) treat it identically to any other
    unavailable provider. The dedicated type lets callers and tests recognize the
    "not authenticated" case specifically.
    """

    def __init__(
        self,
        provider_name: ProviderInstanceName,
        reason: str = "not authenticated",
        short_remediation: str | None = None,
        user_help_text: str | None = None,
        short_reason: str | None = None,
    ) -> None:
        default_help = (
            f"To disable this provider, run:\n"
            f"  mngr config set --scope user providers.{provider_name}.is_enabled false\n"
            f"Or disable the provider backend entirely by removing it from enabled_backends in your config."
        )
        super().__init__(
            provider_name,
            reason,
            user_help_text=user_help_text or default_help,
            short_remediation=short_remediation,
            short_reason=short_reason,
        )


class HostNotFoundError(ProviderError):
    """No host with this ID or name exists."""

    user_help_text = "Use 'mngr list' to see available hosts and agents."

    def __init__(self, provider_name: ProviderInstanceName, host: HostId | HostName) -> None:
        self.host = host
        super().__init__(provider_name, f"Host not found: {host}")


class HostCreationError(ProviderError):
    """Failed to create a host."""


class ImageNotFoundError(HostCreationError):
    """The specified image does not exist or is invalid."""

    def __init__(self, provider_name: ProviderInstanceName, image: ImageReference) -> None:
        self.image = image
        super().__init__(provider_name, f"Image not found: {image}")


class ResourceAllocationError(HostCreationError):
    """Failed to allocate resources for the host."""


class DockerBuildTimeoutError(HostCreationError):
    """Raised when `docker build` exceeds the configured build timeout."""

    def __init__(self, provider_name: ProviderInstanceName, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            provider_name,
            f"docker build timed out after {timeout_seconds} seconds for provider '{provider_name}'.",
        )
        self.user_help_text = (
            f"Increase build_timeout_seconds for this provider, e.g.:\n"
            f"  mngr config set --scope user providers.{provider_name}.build_timeout_seconds 1800"
        )


class DockerRuntimeNotRegisteredError(HostCreationError):
    """Raised when the configured `docker_runtime` is not registered with the Docker daemon.

    Surfaces Docker's native "unknown or invalid runtime name" failure as a
    clean, actionable message instead of a raw `ProcessError` traceback that
    buries the cause inside the full `docker run` command line.
    """

    def __init__(self, provider_name: ProviderInstanceName, runtime_name: str) -> None:
        self.runtime_name = runtime_name
        super().__init__(
            provider_name,
            f"Docker runtime '{runtime_name}' is not registered with the Docker daemon "
            f"for provider '{provider_name}'.",
        )
        self.user_help_text = (
            f"Install and register the '{runtime_name}' runtime with Docker (e.g. gVisor's "
            f"runsc), or select the default runtime by setting docker_runtime to 'runc':\n"
            f"  mngr config set --scope user providers.{provider_name}.docker_runtime runc\n"
            f"or per-invocation:\n"
            f"  MNGR__PROVIDERS__{provider_name.upper()}__DOCKER_RUNTIME=runc"
        )


class HostNameConflictError(ProviderError):
    """A host with this name already exists."""

    user_help_text = "Choose a different host name, or destroy the existing host first with 'mngr destroy'."

    def __init__(self, provider_name: ProviderInstanceName, name: HostName) -> None:
        self.name = name
        super().__init__(provider_name, f"Host name already exists: {name}")


class HostNotRunningError(ProviderError):
    """Host is not in RUNNING state."""

    user_help_text = "Start the host first with 'mngr start <host>'."

    def __init__(self, provider_name: ProviderInstanceName, host_id: HostId, state: HostState) -> None:
        self.host_id = host_id
        self.state = state
        super().__init__(provider_name, f"Host {host_id} is not running (state: {state})")


class HostNotStoppedError(ProviderError):
    """Host is not in STOPPED state."""

    user_help_text = "Stop the host first with 'mngr stop <host>'."

    def __init__(self, provider_name: ProviderInstanceName, host_id: HostId, state: HostState) -> None:
        self.host_id = host_id
        self.state = state
        super().__init__(provider_name, f"Host {host_id} is not stopped (state: {state})")


class SnapshotError(ProviderError):
    """Base class for snapshot-related errors."""


class SnapshotNotFoundError(SnapshotError):
    """No snapshot with this ID exists."""

    user_help_text = "Use 'mngr snapshot list <host>' to see available snapshots."

    def __init__(self, provider_name: ProviderInstanceName, snapshot_id: SnapshotId) -> None:
        self.snapshot_id = snapshot_id
        super().__init__(provider_name, f"Snapshot not found: {snapshot_id}")


class SnapshotsNotSupportedError(SnapshotError):
    """Provider does not support snapshots."""

    user_help_text = (
        "Snapshots are only available for cloud providers like Modal. The local provider does not support snapshots."
    )

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(provider_name, f"Provider {provider_name} does not support snapshots")


class LocalHostNotStoppableError(ProviderError):
    """Raised when attempting to stop the local host."""

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(provider_name, "Cannot stop the local host - it is your local computer")


class LocalHostNotDestroyableError(ProviderError):
    """Raised when attempting to destroy the local host."""

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(provider_name, "Cannot destroy the local host - it is your local computer")


class HostShutdownNotSupportedError(ProviderError):
    """Provider does not support stopping hosts."""

    user_help_text = "Stop the agent without --stop-host, or use a provider that supports stopping hosts."

    def __init__(self, provider_name: ProviderInstanceName) -> None:
        super().__init__(provider_name, f"Provider {provider_name} does not support stopping hosts")


class PluginSpecifierError(MngrError, ValueError):
    """Raised when a plugin specifier is invalid or cannot be resolved."""


class PluginMngrError(MngrError):
    """Raised when a plugin encounters an error during provisioning.

    Plugins should raise this error in the on_before_agent_provisioning hook
    when preconditions are not met (e.g., missing environment variables,
    missing required files).
    """


_DEFAULT_MODAL_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("modal")


class ModalAuthError(ProviderNotAuthorizedError):
    """Modal authentication failed due to missing or invalid token.

    A ``ProviderNotAuthorizedError`` (hence ``ProviderUnavailableError``) so read
    paths (``mngr list`` / ``gc`` / discovery) categorize it as a provider-
    inaccessible / unauthenticated failure consistently with the other cloud
    providers, while preserving the Modal-specific message and remediation.
    """

    def __init__(self, provider_name: ProviderInstanceName = _DEFAULT_MODAL_PROVIDER_NAME) -> None:
        message = (
            "Modal authentication failed. Token missing or invalid. "
            "You can disable the modal plugin by running "
            "'mngr config set --scope user plugins.modal.enabled false', "
            "or by passing --disable-plugin modal to individual commands. "
            "To configure modal credentials, see https://modal.com/docs/reference/modal.config"
        )
        # Initialize the ProviderError base directly so the Modal-specific message and
        # guidance are preserved verbatim (the ProviderUnavailableError base would
        # otherwise rewrite the message into the generic "is not available" shape).
        ProviderError.__init__(self, provider_name, message)
        self.short_reason = "Modal token missing or invalid"
        self.short_remediation = "run `uvx modal token set`"
        # The message already carries full remediation, so no separate help text.
        self.user_help_text = None


class ConfigError(MngrError):
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


class InvalidKeyPathError(ConfigError, ValueError):
    """Raised when a config key path is empty or otherwise malformed."""


class DockerConfigValidationError(ConfigError, ValueError):
    """Raised when Docker provider config fields are mutually inconsistent."""


class UnknownAgentTypeError(ConfigError):
    """Unknown agent type."""

    def __init__(self, agent_type: str) -> None:
        self.agent_type = agent_type
        super().__init__(f"Unknown agent type '{agent_type}'.")
        self.user_help_text = get_plugin_install_hint(agent_type)


class UnknownBackendError(ConfigError):
    """Unknown provider backend."""

    def __init__(self, backend_name: str, registered: list[str]) -> None:
        self.backend_name = backend_name
        self.registered = list(registered)
        registered_str = ", ".join(self.registered) or "(none)"
        message = f"Unknown provider backend: {backend_name}. Registered backends: {registered_str}"
        super().__init__(message)
        self.user_help_text = get_plugin_install_hint(backend_name, kind=PluginKind.PROVIDER)


class NestedTmuxError(MngrError):
    """Cannot attach to tmux session from inside another tmux session."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        super().__init__(
            f"You're already in a tmux session. You can attach to the agent with:\n  tmux attach -t ={session_name}"
        )
        self.user_help_text = (
            "To allow mngr to attach automatically inside tmux, run:\n"
            "  mngr config set --scope user is_nested_tmux_allowed true"
        )


class BinaryNotInstalledError(MngrError):
    """Raised when a required system binary is not installed."""

    def __init__(self, binary: str, purpose: str, install_hint: str) -> None:
        self.user_help_text = install_hint
        super().__init__(f"{binary} is required for {purpose} but was not found on PATH")


class DiscoverySchemaChangedError(MngrError, ValueError):
    """Raised when a discovery event line cannot be validated against the current schema.

    This typically means a field was added, removed, or renamed in a discovery event
    model since the line was written. Callers should treat the on-disk events as stale,
    regenerate via a full discovery (which appends new events in the current schema),
    and retry. If validation fails again after regeneration, the error is real and
    should be surfaced rather than silently dropped.
    """

    def __init__(self, event_type: str, validation_error: str) -> None:
        self.event_type = event_type
        self.validation_error = validation_error
        super().__init__(f"Discovery event of type {event_type!r} does not match current schema: {validation_error}")


class MalformedJsonlLineError(MngrError, ValueError):
    """Raised when a JSONL line is structurally invalid (e.g. not a JSON object, missing required envelope fields).

    The right fix is to track down whichever process is producing the bad line and stop it
    from doing so -- silently skipping corrupt input would just hide the underlying problem.
    Callers that need to tolerate end-of-file partial writes should use
    ``MalformedJsonLineWarner`` (which buffers a malformed line until the next non-empty
    line proves it wasn't a partial write).
    """
