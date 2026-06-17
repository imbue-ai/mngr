import re
from datetime import datetime
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import Mapping
from typing import Self

from pydantic import Field
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt

# === Enums ===


class DockerBuilder(UpperCaseStrEnum):
    """Image builder backend used by the docker provider.

    DOCKER selects native ``docker build``. DEPOT selects ``depot build --load``,
    which uses depot.dev's remote builders with shared layer cache and bypasses
    Docker Hub anonymous pull rate limits.
    """

    DOCKER = auto()
    DEPOT = auto()


class AgentNameStyle(UpperCaseStrEnum):
    """Style for auto-generated agent names."""

    COOLNAME = auto()
    ENGLISH = auto()
    FANTASY = auto()
    SCIFI = auto()
    PAINTERS = auto()
    AUTHORS = auto()
    ARTISTS = auto()
    MUSICIANS = auto()
    ANIMALS = auto()
    SCIENTISTS = auto()
    DEMONS = auto()


class HostNameStyle(UpperCaseStrEnum):
    """Style for auto-generated host names."""

    COOLNAME = auto()
    ASTRONOMY = auto()
    PLACES = auto()
    CITIES = auto()
    FANTASY = auto()
    SCIFI = auto()
    PAINTERS = auto()
    AUTHORS = auto()
    ARTISTS = auto()
    MUSICIANS = auto()
    SCIENTISTS = auto()


class LogLevel(UpperCaseStrEnum):
    """Log verbosity level."""

    TRACE = auto()
    DEBUG = auto()
    BUILD = auto()
    INFO = auto()
    WARN = auto()
    ERROR = auto()
    NONE = auto()


class IdleMode(UpperCaseStrEnum):
    """Mode for determining when host is considered idle."""

    IO = auto()
    USER = auto()
    AGENT = auto()
    SSH = auto()
    CREATE = auto()
    BOOT = auto()
    START = auto()
    RUN = auto()
    CUSTOM = auto()
    DISABLED = auto()


class TmuxWindowSize(UpperCaseStrEnum):
    """Resize policy for an agent's tmux window (tmux ``window-size`` option).

    The lowercase of each value is exactly the token tmux accepts. ``MANUAL``
    pins the window to its configured size and never auto-resizes to attached
    clients; ``LATEST`` (tmux's own default) sizes to the most recent client;
    ``LARGEST`` / ``SMALLEST`` size to the largest / smallest attached client.
    """

    MANUAL = auto()
    LATEST = auto()
    LARGEST = auto()
    SMALLEST = auto()


class TmuxWidth(PositiveInt):
    """Width, in columns, of an agent's tmux window. Must be > 0."""

    ...


class TmuxHeight(PositiveInt):
    """Height, in rows, of an agent's tmux window. Must be > 0."""

    ...


class ActivitySource(UpperCaseStrEnum):
    """Sources of activity for idle detection."""

    CREATE = auto()
    BOOT = auto()
    START = auto()
    SSH = auto()
    PROCESS = auto()
    AGENT = auto()
    USER = auto()


class BootstrapMode(UpperCaseStrEnum):
    """Bootstrap behavior for missing tools."""

    SILENT = auto()
    WARN = auto()
    FAIL = auto()


class LifecycleHook(UpperCaseStrEnum):
    """Available lifecycle hooks."""

    INITIALIZE = auto()
    ON_CREATE = auto()
    UPDATE_CONTENT = auto()
    POST_CREATE = auto()
    POST_START = auto()
    POST_ATTACH = auto()


class OutputFormat(UpperCaseStrEnum):
    """Output format mode."""

    HUMAN = auto()
    JSON = auto()
    JSONL = auto()


class ErrorBehavior(UpperCaseStrEnum):
    """Behavior when encountering errors during operations."""

    ABORT = auto()
    CONTINUE = auto()


class CleanupAction(UpperCaseStrEnum):
    """Action to perform on selected agents during cleanup."""

    DESTROY = auto()
    STOP = auto()


class TransferMode(UpperCaseStrEnum):
    """How to transfer the project into the agent's work directory.

    NONE: Run in-place, no transfer.
    RSYNC: Transfer files via rsync (non-git projects only).
    GIT_MIRROR: Push all local branches and tags via git (git projects, works locally and remotely).
    GIT_WORKTREE: Create a git worktree (git projects, local agents only).
    """

    NONE = auto()
    RSYNC = auto()
    GIT_MIRROR = auto()
    GIT_WORKTREE = auto()


class UncommittedChangesMode(UpperCaseStrEnum):
    """Mode for handling uncommitted changes in the destination during sync operations."""

    STASH = auto()
    CLOBBER = auto()
    MERGE = auto()
    FAIL = auto()


class SyncDirection(UpperCaseStrEnum):
    """Direction for file synchronization in pair mode."""

    FORWARD = auto()
    REVERSE = auto()
    BOTH = auto()


class ConflictMode(UpperCaseStrEnum):
    """Conflict resolution mode for pair mode sync."""

    NEWER = auto()
    SOURCE = auto()
    TARGET = auto()
    ASK = auto()


class PluginTier(UpperCaseStrEnum):
    """Whether a plugin works standalone or depends on another plugin.

    INDEPENDENT: works out of the box (may have a signal for binary detection).
    DEPENDENT: requires another plugin's signal to be relevant (e.g.,
               fixme_fairy depends on claude).
    """

    INDEPENDENT = auto()
    DEPENDENT = auto()


class PluginKind(UpperCaseStrEnum):
    """What category of mngr extension a plugin provides.

    Shared by the install-hint helper and (when it lands) the
    ``mngr plugin list --kind`` CLI filter. Member names match the
    project's user-facing vocabulary; convert kebab-case CLI strings
    (``agent-type``, ``provider``) at the CLI boundary.
    """

    AGENT_TYPE = auto()
    PROVIDER = auto()


# === ID Types ===


class HostState(UpperCaseStrEnum):
    """The lifecycle state of a host."""

    BUILDING = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    STOPPED = auto()
    PAUSED = auto()
    CRASHED = auto()
    FAILED = auto()
    DESTROYED = auto()
    UNAUTHENTICATED = auto()
    # The provider that owns this host could not be accessed during the most recent discovery attempt,
    # so the host's actual state is unknown. Distinct from None on HostDetails.state (which means
    # "not observed / not applicable"). Emitted by AgentObserver when its provider errored.
    UNKNOWN = auto()


class AgentLifecycleState(UpperCaseStrEnum):
    """The lifecycle state of an agent."""

    STOPPED = auto()
    RUNNING = auto()
    WAITING = auto()
    REPLACED = auto()
    # this happens when an agent is running but our configuration doesn't have an entry for that agent type (e.g. if it was launched remotely or by someone else)
    # without the config, it can be hard to tell whether the agent is still running or not, because we don't know the process name to expect
    RUNNING_UNKNOWN_AGENT_TYPE = auto()
    DONE = auto()
    # The provider that owns this agent could not be accessed during the most recent discovery attempt,
    # so the agent's actual state is unknown. Emitted by AgentObserver for previously-tracked agents
    # whose provider just failed discovery. Sticky: an agent leaves UNKNOWN only by reappearing in a
    # snapshot or being explicitly destroyed.
    UNKNOWN = auto()


class WaitingReason(UpperCaseStrEnum):
    """Why an agent in the WAITING lifecycle state is waiting.

    Reported as the ``waiting_reason`` field by agent plugins (see the
    ``agent_field_generators`` hook). Shared across plugins so the codex and claude
    implementations agree on the vocabulary; see ``classify_waiting_reason`` in
    ``imbue.mngr.hosts.common`` for the shared rule that produces it.
    """

    # Blocked on a tool-approval dialog, waiting for the user to respond.
    PERMISSIONS = auto()
    # Idle with its turn complete, waiting for the user's next message.
    END_OF_TURN = auto()


class AgentId(RandomId):
    """Unique identifier for an agent."""

    PREFIX = "agent"


class HostId(RandomId):
    """Unique identifier for a host."""

    PREFIX = "host"


class SnapshotId(NonEmptyStr):
    """Unique identifier for a snapshot."""


class VolumeId(RandomId):
    """Unique identifier for a volume."""

    PREFIX = "vol"


class InvalidName(ValueError):
    pass


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


class SafeName(NonEmptyStr):
    """Base type for human-readable names used in filesystem paths and shell commands.

    Must be alphanumeric with dashes and underscores allowed in the middle, must not start
    or end with a dash. This is enforced because these names appear in
    filesystem paths, tmux session names, and other contexts where special
    characters like ``/`` would break things.
    """

    def __new__(cls, value: str) -> Self:
        value = value.strip()
        if not _SAFE_NAME_RE.match(value):
            raise InvalidName(
                f"{cls.__name__} must be alphanumeric (with dashes and underscores allowed in the middle): '{value}'"
            )
        return super().__new__(cls, value)


class ProviderInstanceName(SafeName):
    """Name of a provider instance."""


LOCAL_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("local")

DEFAULT_BRANCH_PREFIX: Final[str] = "mngr/"


def default_branch_name(agent_name: "AgentName", prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """Build the default branch name for an agent."""
    return f"{prefix}{agent_name}"


class ProviderBackendName(SafeName):
    """Name of a provider backend."""


class AgentName(SafeName):
    """Human-readable name for an agent."""


class HostName(SafeName):
    """Human-readable name for a host.

    Host names never contain dots: the dot is reserved as the deterministic
    separator in ``HOST.PROVIDER`` host addresses (see ``api/addresses.py``).
    """


# A "name or id" reference where the parser couldn't disambiguate at parse time;
# downstream code matches by ID first, then falls back to name.
# IDs are listed first so pydantic's union resolution prefers the more specific
# type when validating string inputs that already match an ID prefix.
AgentNameOrId = AgentId | AgentName
HostNameOrId = HostId | HostName


# === Parsed address types ===
#
# These FrozenModel shapes are produced by the parsers in
# ``api/addresses.py`` and consumed across the codebase, including by
# ``config/data_types.py``. They live here in ``primitives`` because the
# config layer needs to type its CLI option fields with them, and the import
# layering does not allow ``config`` to depend on ``api``.


class HostAddress(FrozenModel):
    """A parsed ``HOST[.PROVIDER]`` string.

    The host component is required. The bare ``.PROVIDER`` form -- which
    appears only in ``mngr create NAME@.PROVIDER`` to mean "create a new host
    on this provider" -- is represented by the flat fields on
    :class:`NewAgentLocation` instead, not by a HostAddress with no host.
    """

    host: HostNameOrId = Field(description="Host name or ID")
    provider: ProviderInstanceName | None = Field(
        default=None, description="Provider instance name (the ``.PROVIDER`` qualifier)"
    )

    def matches(self, other: "HostAddress") -> bool:
        """True if every component of ``self`` matches the corresponding component of ``other``.

        ``self`` is read as a constraint (e.g. parsed from a ``--host`` flag);
        ``other`` is the concrete address being tested. Provider matching is
        only enforced when ``self.provider`` is set, so a constraint of just
        ``HOST`` matches every concrete host with that name regardless of
        provider.
        """
        if self.host != other.host:
            return False
        if self.provider is not None and self.provider != other.provider:
            return False
        return True

    def __str__(self) -> str:
        if self.provider is not None:
            return f"{self.host}.{self.provider}"
        return str(self.host)


class AgentAddress(FrozenModel):
    """A parsed ``NAME[@HOST[.PROVIDER]]`` string.

    The agent component is required; without it, this is not an agent address.
    Use :class:`HostAddress` for ``@HOST.PROVIDER`` (no agent) or
    :class:`HostLocationAddress` for ``[NAME[@HOST[.PROVIDER]]][:PATH]`` syntax.
    """

    agent: AgentNameOrId = Field(description="Agent name or ID (required)")
    host: HostAddress | None = Field(default=None, description="Optional host disambiguator")

    def __str__(self) -> str:
        if self.host is None:
            return str(self.agent)
        return f"{self.agent}@{self.host}"


# A text-disambiguated agent-or-host argument. The textual rules are:
# - A leading "@" forces host parsing.
# - An identifier with HostId shape (``host-...``) is treated as a host.
# - Otherwise the input is tried as an agent first, then as a host.
# Used by commands whose top-level positional may refer to either kind of
# entity: ``mngr event``, ``mngr transcript``, ``mngr snapshot create``,
# ``mngr snapshot list``, ``mngr wait``. See
# :func:`imbue.mngr.api.address_parsers.parse_agent_or_host_address`.
AgentOrHostAddress = AgentAddress | HostAddress


class NewAgentLocation(FrozenModel):
    """A parsed ``[NAME][@[HOST][.PROVIDER]][:PATH]`` string.

    Used as the positional argument of ``mngr create``. The agent name is
    optional (omitted means "auto-generate"). The host and provider components
    are flat-optional rather than a nested :class:`HostAddress`, because
    ``mngr create`` is the only context in which the bare ``.PROVIDER`` form
    is meaningful -- it means "create a new host on this provider with an
    auto-generated name". When the user types ``HOST.PROVIDER``, both fields
    are set; ``HOST`` alone sets just ``host_name``; ``.PROVIDER`` alone sets
    just ``provider_name``. The trailing ``:PATH`` overrides the agent's
    default work-directory location.

    ``name`` is :class:`AgentName`, not :class:`AgentNameOrId` -- the agent
    doesn't yet exist when ``mngr create`` runs, so referring to it by ID is
    meaningless.
    """

    name: AgentName | None = Field(default=None, description="Optional explicit agent name")
    host_name: HostNameOrId | None = Field(default=None, description="Optional host name or ID")
    provider_name: ProviderInstanceName | None = Field(
        default=None, description="Optional provider instance name (the ``.PROVIDER`` qualifier)"
    )
    path: Path | None = Field(default=None, description="Optional explicit work-directory path inside the host")


class HostLocationAddress(FrozenModel):
    """A location that lives on some host: ``[NAME[@HOST[.PROVIDER]]][:PATH]`` or a bare path.

    Used wherever a CLI command needs to designate "a place on any host" -- the
    source for ``mngr create --from``/``mngr pair``, the source/destination for
    ``mngr rsync``, and the target for ``mngr git push``/``mngr git pull``. The
    host may be local or remote; "hosted" captures both.

    Every component is optional. The four meaningful shapes (in addition to a
    bare path string) are:

    - ``AGENT`` -> agent's host + agent's work_dir
    - ``AGENT:PATH`` -> agent's host + explicit ``PATH``
    - ``@HOST[.PROVIDER]:PATH`` -> explicit host + ``PATH``
    - ``:PATH`` -> local path

    A bare path string starting with ``/``, ``./``, ``~/``, or ``../`` is also
    parsed directly into ``path`` as a convenience.
    """

    agent: AgentNameOrId | None = Field(default=None, description="Optional agent name or ID")
    host: HostAddress | None = Field(default=None, description="Optional host")
    path: Path | None = Field(default=None, description="Optional path")
    has_trailing_path_slash: bool = Field(
        default=False,
        description=(
            "True if the user-typed PATH ended with ``/``. ``Path`` strips trailing slashes, "
            "so this flag is the only way to preserve rsync's contents-vs-child semantics."
        ),
    )


class AgentTypeName(SafeName):
    """Type name for an agent (e.g., claude, codex)."""


class UserId(NonEmptyStr):
    """Unique user identifier for namespacing provider resources."""


class PluginName(NonEmptyStr):
    """Name of a plugin."""


class ImageReference(NonEmptyStr):
    """Reference to a container or VM image."""


class CommandString(NonEmptyStr):
    """Command string to be executed."""


class SnapshotName(str):
    """Human-readable name for a snapshot."""

    def __new__(cls, value: str) -> Self:
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )


class CertifiedDataError(Exception):
    """Raised when certified_data contains an unexpected type for a field."""


class SSHInfo(FrozenModel):
    """SSH connection information for a remote host."""

    user: str = Field(description="SSH username")
    host: str = Field(description="SSH hostname")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to SSH private key")
    command: str = Field(description="Full SSH command to connect")


class DiscoveredHost(FrozenModel):
    """Lightweight host data collected during discovery (without connecting to the host)."""

    host_id: HostId = Field(description="Unique identifier for the host")
    host_name: HostName = Field(description="Human-readable name of the host")
    provider_name: ProviderInstanceName = Field(description="Name of the provider instance that owns the host")
    host_state: "HostState | None" = Field(
        default=None, description="Host lifecycle state, if known at discovery time"
    )


class DiscoveredAgent(FrozenModel):
    """Lightweight agent data collected during discovery (without connecting to the host).

    This class provides access to agent data that can be retrieved without requiring
    the host to be online. The certified_data field contains the raw data.json contents,
    and property methods provide convenient typed access to common fields.
    """

    host_id: HostId
    agent_id: AgentId
    agent_name: AgentName
    provider_name: ProviderInstanceName
    certified_data: Mapping[str, Any] = Field(default_factory=dict)

    @property
    def agent_type(self) -> "AgentTypeName | None":
        """Return the agent type, or None if not available."""
        type_value = self.certified_data.get("type")
        if type_value is not None:
            return AgentTypeName(type_value)
        return None

    @property
    def work_dir(self) -> Path | None:
        """Return the agent's working directory, or None if not available."""
        work_dir_value = self.certified_data.get("work_dir")
        if work_dir_value is not None:
            return Path(work_dir_value)
        return None

    @property
    def command(self) -> "CommandString | None":
        """Return the command used to start this agent, or None if not available."""
        command_value = self.certified_data.get("command")
        if command_value is not None:
            return CommandString(command_value)
        return None

    @property
    def create_time(self) -> datetime | None:
        """Return the agent creation time, or None if not available."""
        create_time_value = self.certified_data.get("create_time")
        if create_time_value is not None:
            if isinstance(create_time_value, datetime):
                return create_time_value
            # Handle ISO format string
            return datetime.fromisoformat(create_time_value)
        return None

    @property
    def start_on_boot(self) -> bool:
        """Return whether this agent should start automatically on host boot."""
        return bool(self.certified_data.get("start_on_boot", False))

    @property
    def created_branch_name(self) -> str | None:
        """Return the git branch name that was created for this agent, or None if not set."""
        match self.certified_data.get("created_branch_name"):
            case str(value):
                return value
            case None:
                return None
            case unexpected:
                raise CertifiedDataError(f"Expected str or None for created_branch_name, got {type(unexpected)}")

    @property
    def labels(self) -> dict[str, str]:
        """Return the labels attached to this agent."""
        return dict(self.certified_data.get("labels", {}))
