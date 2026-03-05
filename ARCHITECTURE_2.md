## ARCHITECTURE OVERVIEW: MNG MONOREPO

Based on a thorough exploration of the core abstractions and data types in the mng codebase, here is a detailed architecture overview:

### Core Domain Model

The system manages **AI agents** running on **hosts** provided by **cloud providers**. The three primary domain objects are:

1. **Agents** (`AgentInterface`): Autonomous AI processes (Claude, Codex, etc.) that run in tmux sessions on hosts with their own working directories, environment variables, and configuration. Each agent has:
   - `id` (AgentId): UUID-based unique identifier with "agent-" prefix
   - `name` (AgentName): Human-readable name
   - `agent_type` (AgentTypeName): Type classifier (claude, codex, etc.)
   - `work_dir` (Path): Working directory on the host
   - `create_time` (datetime): Creation timestamp
   - `host_id` (HostId): Reference to the host running it

2. **Hosts** (`HostInterface` / `OnlineHostInterface`): Machines (local, Docker, Modal, AWS, etc.) that run agents and store their state. Each host has:
   - `id` (HostId): UUID-based identifier with "host-" prefix
   - `name` (HostName): Human-readable name
   - Agent references tracking all agents on the host
   - Certified data (metadata stored in data.json)
   - Activity configuration for idle detection

3. **Provider Instances** (`ProviderInstanceInterface`): Configured endpoints that create and manage hosts (e.g., "local", "modal-prod"). They are created by provider backends and are stateful representations of cloud provider connections.

### Primitives and Type System

The codebase aggressively uses constrained primitive types to encode domain knowledge at compile time:

**ID Types** (all inherit from `RandomId`):
- `AgentId`, `HostId`, `VolumeId`, `SnapshotId`
- All use UUID4-based hex strings with optional prefixes
- Validated at construction time; raise `InvalidRandomIdError` on invalid input

**Constrained String Types**:
- `NonEmptyStr`: Cannot be empty or whitespace-only
- `AgentName`, `HostName`, `AgentTypeName`: Semantic domain names (inherit from `NonEmptyStr`)
- `CommandString`: Command to execute (cannot be empty)
- `ProviderInstanceName`, `ProviderBackendName`, `PluginName`: Named entities

**Constrained Numeric Types**:
- `NonNegativeInt`, `PositiveInt`: Integers with range constraints
- `NonNegativeFloat`, `PositiveFloat`: Floats with range constraints
- `Probability`: Float constrained to [0.0, 1.0] (raises `InvalidProbabilityError`)
- `SizeBytes`: Non-negative integer for sizes

**Enums** (all inherit from `UpperCaseStrEnum`):
- `HostState`: BUILDING, STARTING, RUNNING, STOPPING, STOPPED, PAUSED, CRASHED, FAILED, DESTROYED, UNAUTHENTICATED
- `AgentLifecycleState`: STOPPED, RUNNING, WAITING, REPLACED, DONE
- `IdleMode`: IO, USER, AGENT, SSH, CREATE, BOOT, START, RUN, CUSTOM, DISABLED
- `ActivitySource`: CREATE, BOOT, START, SSH, PROCESS, AGENT, USER
- `WorkDirCopyMode`: COPY, CLONE, WORKTREE
- Various other behavioral enums

### Immutability and Model Types

The codebase follows a stateless, functional style using Pydantic models:

- **`FrozenModel`** (from `imbue_common`): Immutable models with `frozen=True`. Used for data transfer objects, configuration, certified host/agent data. Provides `model_copy_update()` for type-safe field updates.

- **`MutableModel`** (from `imbue_common`): Mutable models used for interface implementations that need to maintain internal state (e.g., HostInterface, AgentInterface implementations). Allow mutation of non-frozen fields.

All models forbid extra fields (`extra="forbid"`) and support Pydantic serialization for JSON/file persistence.

### Interfaces and Protocols

The system is organized around abstract interfaces that define contracts for implementations:

**1. Host Interface** (`HostInterface`, `OnlineHostInterface`):
   - Core primitives: execute commands, read/write files, get modification times
   - Activity tracking: record and query activity times for idle detection
   - Agent management: create, rename, destroy, start, stop agents
   - Certified data management: get/set host metadata (stored in data.json)
   - Plugin data: per-plugin extensible data storage
   - Cooperative locking: distributed lock acquisition for coordination

**2. Agent Interface** (`AgentInterface`):
   - Certified data: get/set command, permissions, labels, start-on-boot flag
   - Lifecycle state: query running state, wait for readiness
   - Interaction: send messages, capture tmux pane content
   - Activity tracking: record and query activity times per agent
   - Plugin data: certified (persistent in data.json) and reported (temporary files)
   - Environment variables: get/set per-agent environment
   - Provisioning hooks: `on_before_provisioning()`, `provision()`, `on_after_provisioning()`, `on_destroy()`

**3. Provider Backend Interface** (`ProviderBackendInterface`):
   - Stateless factory for creating provider instances
   - Returns provider metadata (name, description, config class, help text)
   - Single method: `build_provider_instance()`

**4. Provider Instance Interface** (`ProviderInstanceInterface`):
   - Host lifecycle: `create_host()`, `stop_host()`, `start_host()`, `destroy_host()`
   - Host discovery: `get_host()`, `list_hosts()`, `load_agent_refs()`
   - Snapshot management: `create_snapshot()`, `list_snapshots()`, `delete_snapshot()`
   - Volume management: `list_volumes()`, `delete_volume()`, `get_volume_for_host()`
   - Host mutation: `rename_host()`, `set_host_tags()`, `add_tags_to_host()`
   - Connector: `get_connector()` returns pyinfra Host for command execution
   - Agent data persistence: `persist_agent_data()`, `remove_persisted_agent_data()`

**5. Volume Interface** (`Volume`):
   - File operations: `listdir()`, `read_file()`, `write_files()`, `remove_file()`, `remove_directory()`
   - Scoping: `scoped(prefix)` returns a new Volume with path prefix prepended
   - Base implementation: `BaseVolume` provides default scoping via `ScopedVolume` decorator
   - Concrete implementations: ModalVolume, LocalVolume, etc. inherit from `BaseVolume`

### Key Data Structures

**Certified Host Data** (`CertifiedHostData`):
- Immutable data stored in host data.json
- Includes: creation/update timestamps, idle configuration, generated work directories, image reference, user tags, snapshots, build/failure logs

**Certified Agent Data** (`AgentReference`):
- Immutable reference accessible without requiring host to be online
- Includes: agent type, work directory, command, creation time, start-on-boot flag, permissions, labels
- Properties expose certified_data fields with type safety

**Agent Creation Options** (`CreateAgentOptions`):
- Comprehensive configuration for agent provisioning
- Nested options: `AgentGitOptions`, `AgentEnvironmentOptions`, `AgentLifecycleOptions`, `AgentPermissionsOptions`, `AgentLabelOptions`, `AgentProvisioningOptions`
- File transfer specs: `FileTransferSpec` (local→remote files during provisioning)

**Activity Configuration** (`ActivityConfig`):
- `idle_timeout_seconds`: How long before host is considered idle
- `activity_sources`: Tuple of ActivitySource that count toward keeping host active
- Computed `idle_mode` field derived from activity sources

### Design Patterns

**1. Type-Driven Design**:
- Extensive use of domain-specific primitive types to encode constraints at the type level
- Pydantic validation with custom `__get_pydantic_core_schema__()` methods for serialization
- Use of `Field(frozen=True)` on immutable fields of mutable models

**2. Interface-Based Architecture**:
- All plugins and provider implementations work through abstract interfaces
- Providers are stateless factories (ProviderBackend) creating stateful instances (ProviderInstance)
- Host and Agent interfaces define operations without specifying implementation details

**3. Immutable Data Transfer**:
- Configuration and data classes use FrozenModel for immutability
- Mutable state is confined to implementation classes (Host/Agent implementations)
- Model updates via `model_copy()` or `model_copy_update()` for functional updates

**4. Scoping and Composition**:
- Volume interface supports scoping through decorator pattern (`ScopedVolume`)
- Allows multiple logical volumes to scope the same backing store
- Agent-level volumes scoped from host-level volumes via path prefix

**5. Lazy Property Access**:
- Agent/Host references provide property methods for accessing certified data
- Only computed when accessed, avoiding unnecessary type conversions

**6. Hook-Based Extensibility**:
- Agents implement provisioning lifecycle hooks (`on_before_provisioning`, `provision`, `on_after_provisioning`, `on_destroy`)
- Plugins can declare file transfers via `get_provision_file_transfers()`
- Allows clean separation between core provisioning and agent-specific setup

### Module Organization

```
libs/mng/
├── imbue/mng/
│   ├── interfaces/           # Core abstract interfaces
│   │   ├── data_types.py     # Data structures and enums
│   │   ├── host.py           # HostInterface
│   │   ├── agent.py          # AgentInterface
│   │   ├── provider_backend.py
│   │   ├── provider_instance.py
│   │   └── volume.py         # Volume interface
│   ├── primitives.py         # Domain-specific ID and string types
│   ├── api/data_types.py     # Higher-level API structures
│   ├── config/data_types.py  # Configuration structures
│   └── ...

libs/imbue_common/
└── imbue/imbue_common/
    ├── primitives.py         # Base constrained types (NonEmptyStr, Probability, etc.)
    ├── ids.py                # RandomId base class
    ├── enums.py              # UpperCaseStrEnum
    ├── frozen_model.py       # FrozenModel base class
    ├── mutable_model.py      # MutableModel base class
    └── ...
```

### Type System Usage for Correctness

The codebase uses the type system extensively to prevent errors:

1. **Constrained Primitives**: Invalid values are caught at construction time
2. **Domain Semantics**: Different ID types prevent mixing AgentId with HostId
3. **Immutability**: FrozenModel prevents accidental mutations
4. **Pydantic Validation**: Custom validators enforce business logic constraints
5. **Field Freezing**: Critical fields like IDs are `frozen=True` even in MutableModel
6. **Computed Fields**: Read-only derived properties via `@computed_field`

This design trades runtime flexibility for compile-time correctness guarantees, ensuring the system fails fast when invalid data is constructed rather than propagating errors downstream.