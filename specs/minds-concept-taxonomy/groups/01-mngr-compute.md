# Group 1: mngr compute core

---

## 1. Providers

### 1.1 Canonical Definition

There is a **two-level abstraction**: a stateless factory (**ProviderBackend**) and a stateful configured endpoint (**ProviderInstance**).

**ProviderBackendInterface** — `libs/mngr/imbue/mngr/interfaces/provider_backend.py:12`
```python
class ProviderBackendInterface(MutableModel, ABC):
    """Interface for provider backends.
    Provider backends are stateless factories that create provider instances.
    All methods are static since backends have no instance state.
    """
```

**ProviderInstanceInterface** — `libs/mngr/imbue/mngr/interfaces/provider_instance.py:291`
```python
class ProviderInstanceInterface(MutableModel, ABC):
    """A ProviderInstance is a configured endpoint that creates and manages hosts."""
    name: ProviderInstanceName
    host_dir: Path
    mngr_ctx: MngrContext
```

**Plugin registration hookspec** — `libs/mngr/imbue/mngr/plugins/hookspecs.py:32`
```python
@hookspec
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]] | None:
```

Each concrete provider plugin implements `hookimpl` on `register_provider_backend()`, returning `(BackendClass, ConfigClass)`.

### 1.2 Concrete Providers (All Enumerated)

| Backend Name | ProviderBackend class | ProviderInstance class | Location |
|---|---|---|---|
| `"local"` | `LocalProviderBackend` | `LocalProviderInstance` | `libs/mngr/imbue/mngr/providers/local/backend.py:18` |
| `"docker"` | `DockerProviderBackend` | `DockerProviderInstance` | `libs/mngr/imbue/mngr/providers/docker/backend.py:31` |
| `"ssh"` | `SSHProviderBackend` | `SSHProviderInstance` | `libs/mngr/imbue/mngr/providers/ssh/backend.py:19` |
| `"modal"` | `ModalProviderBackend` | `ModalProviderInstance` | `libs/mngr_modal/imbue/mngr_modal/backend.py:245` |
| `"lima"` | `LimaProviderBackend` | `LimaProviderInstance` | `libs/mngr_lima/imbue/mngr_lima/backend.py:17` |
| `"vultr"` | `VultrProviderBackend` | `VultrProvider` (subclass of `VpsDockerProvider`) | `libs/mngr_vultr/imbue/mngr_vultr/backend.py:86` |
| `"ovh"` | `OvhProviderBackend` | `OvhProvider` (subclass of `VpsDockerProvider`) | `libs/mngr_ovh/imbue/mngr_ovh/backend.py:603` |
| `"aws"` | `AwsProviderBackend` | `AwsProvider` (subclass of `VpsDockerProvider`) | `libs/mngr_aws/imbue/mngr_aws/backend.py:227` |
| `"gcp"` | `GcpProviderBackend` | `GcpProvider` (subclass of `VpsDockerProvider`) | `libs/mngr_gcp/imbue/mngr_gcp/backend.py:290` |
| `"imbue_cloud"` | `ImbueCloudProviderBackend` | `ImbueCloudProvider` (subclass of `BaseProviderInstance`) | `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/backend.py:22` |
| `"vps_docker"` | — (no standalone backend; shared base class) | `VpsDockerProvider` | `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:413` |

Note: `vps_docker` is a shared provider base used by `vultr`, `ovh`, `aws`, and `gcp`; it does NOT register its own backend name. All four subclass `VpsDockerProvider`.

The `local` provider name is also defined as a compile-time constant:
```python
LOCAL_PROVIDER_NAME: Final[ProviderInstanceName] = ProviderInstanceName("local")
# libs/mngr/imbue/mngr/primitives.py:332
```

### 1.3 Competing / Multiple Definitions

- `ProviderBackendInterface` lives in `interfaces/provider_backend.py` and `BaseProviderInstance` (a concrete partial implementation) lives in `providers/base_provider.py`. These are at different layers: the interface is pluggy-visible; the base class is an internal implementation helper.
- The name `"local"` appears as both `LOCAL_BACKEND_NAME: ProviderBackendName = ProviderBackendName("local")` (`providers/local/backend.py:15`) and `LOCAL_PROVIDER_NAME: ProviderInstanceName = ProviderInstanceName("local")` (`primitives.py:332`). These refer to different concepts (backend type vs. default instance name).

### 1.4 Terminology Variants

| Term | Type | Where |
|---|---|---|
| `ProviderBackend`, `provider backend` | Stateless factory class | `interfaces/provider_backend.py`, hookspecs, CLI help strings |
| `ProviderInstance`, `provider instance` | Stateful configured endpoint | `interfaces/provider_instance.py`, config layer |
| `provider` | Colloquial for either | User-facing CLI docs, config TOML sections |
| `ProviderBackendName` | `SafeName` subclass; the string key for a backend type | `primitives.py:342` |
| `ProviderInstanceName` | `SafeName` subclass; the string key for an instance in config | `primitives.py:328` |
| `PluginKind.PROVIDER` | Enum value for provider-type plugins | `primitives.py:238` |
| `backend` | Field name in `ProviderInstanceConfig.backend` (references backend by name) | `config/data_types.py:488` |

### 1.5 Definition Ambiguities / Inconsistencies

- The term **"provider"** is used at both levels interchangeably in docs, comments and CLI strings. In config TOML the `[providers.my_instance] backend = "docker"` syntax correctly separates instance from backend, but conversational text conflates them (e.g., "the docker provider" can mean the backend or any instance using it).
- `VpsDockerProvider` acts as a shared abstract base for Vultr, OVH, AWS, and GCP but lives in `libs/mngr_vps_docker/` as if it were a concrete provider — it has no `register_provider_backend()` hookimpl and is not usable directly by end users.
- `VultrProvider` subclasses `VpsDockerProvider` but is named `VultrProvider` not `VultrProviderInstance`/`VultrProviderBackend`, inconsistent with other provider naming.

### 1.6 Doc/Code Divergences

None found for this concept. The `ImbueCloudProviderBackend.get_description()` return value is "Imbue Cloud (leased pool hosts via remote_service_connector)" — this accurately matches the code at `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/backend.py:31`.

### 1.7 Recommended Canonical Term + Definition

- **ProviderBackend**: stateless factory (one per backend type, registered via pluggy hookimpl). Identified by `ProviderBackendName` (e.g., `"docker"`, `"modal"`).
- **ProviderInstance**: the running, configured object that manages a pool of hosts. Identified by `ProviderInstanceName` (e.g., `"docker"`, `"my-docker-gpu"`, `"local"`). Config entries at `[providers.<name>]` create instances.
- The term **"provider"** should be reserved for `ProviderInstance` in user-facing text; "provider backend" (or "backend") should refer to the factory.
- Rename `VultrProvider` to `VultrProviderInstance` (or `VultrProvider` stays but the code/naming inconsistency should be documented).

---

## 2. Regions

### 2.1 Canonical Definition

There is **no shared region abstraction in mngr core**. Each cloud provider independently defines and handles "region" as a plain `str` or `str | None` field on its own provider config. There is no `RegionName` type, no region enum, and no shared validation in `libs/mngr/`.

The only near-canonical region validation lives in `mngr_imbue_cloud`:
```python
KNOWN_OVH_US_REGIONS: Final[frozenset[str]] = frozenset({"US-EAST-VA", "US-WEST-OR"})
# libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/primitives.py:15
```
This validates the `-b region=` build arg at `mngr create` time for imbue_cloud, not for other providers.

### 2.2 All Usages (grouped by provider)

**Modal** — `libs/mngr_modal/imbue/mngr_modal/config.py:98`
```python
default_region: str | None = Field(default=None,
    description="Default region (e.g., 'us-east'). None lets Modal choose.")
```
Used as `--region` start arg passed to Modal's sandbox API (`instance.py:259`, `1351`, `1374`, `1783`, `2044`).

**Vultr** — `libs/mngr_vultr/imbue/mngr_vultr/config.py:26`
```python
default_region: str = Field(default="ewr",
    description="Default Vultr region (e.g., 'ewr' for New Jersey)")
```
Also exposed as a `--vultr-region=REGION` per-host build arg in the Vultr create path (renamed from the retired shared `--vps-region=` prefix).

**OVH** — `libs/mngr_ovh/imbue/mngr_ovh/config.py:69`
```python
default_region: str = Field(default=_DEFAULT_REGION,  # _DEFAULT_REGION = "US-EAST-VA"
    description="Default VPS datacenter (e.g. US-EAST-VA, US-WEST-OR for US accounts).")
```
The default `"US-EAST-VA"` (`config.py:14`) matches one of `KNOWN_OVH_US_REGIONS`. Exposed as a `--ovh-region=` / `--ovh-datacenter=` per-host build arg.

**AWS** — `libs/mngr_aws/imbue/mngr_aws/config.py:90`
```python
default_region: str = Field(default="us-east-1",
    description="Default AWS region (e.g., 'us-east-1').")
```
EC2's API is per-region, so minds writes one `[providers.aws-<region>]` block per configured region at startup; the create address selects the region-specific provider. Exposed as a `--aws-region=` per-host build arg.

**GCP** — `libs/mngr_gcp/imbue/mngr_gcp/config.py:106` (region) and `:114` (zone)
```python
default_region: str | None = Field(default=None, ...)  # validates the resolved zone; derived from zone when unset
default_zone: str | None = Field(default=None, ...)     # GCE VMs are zonal, e.g. "us-west1-a"; falls back to "us-west1-a"
```
GCE VMs are zonal (`<region>-<suffix>`), so the zone is primary and the region is derived from it via `resolve_zone_and_region()` (`config.py:258`). The per-host placement knob is `--gcp-zone=ZONE` (not a region flag), threaded through `ParsedVpsBuildOptions.region` which the GCP client interprets as the zone.

**imbue_cloud** — `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/data_types.py:83`
```python
region: str | None = Field(default=None,
    description="``-b region=<dc>`` hard region requirement: only lease a host in this OVH datacenter...")
```
Validated against `KNOWN_OVH_US_REGIONS` in `parse_imbue_cloud_build_args()`. Passed to `client.lease_host(..., region=region)`.

**minds (desktop client)** — `apps/minds/imbue/minds/desktop_client/agent_creator.py:677`
```python
if region:
    mngr_command.extend(["-b", f"--vultr-region={region}"])  # LaunchMode.VULTR
```
`_build_mngr_create_command` threads the user-picked region per launch mode (`match launch_mode` at `agent_creator.py:664`): `LaunchMode.VULTR` → `--vultr-region=` (line 678), `LaunchMode.AWS` → `--aws-region=` (line 687), `LaunchMode.IMBUE_CLOUD` → `region=` (line 712). The old `LaunchMode.CLOUD` member was renamed to `VULTR`.

**Local / Docker / Lima / SSH** — No region concept.

### 2.3 Competing / Multiple Definitions

- OVH uses the format `"US-EAST-VA"` (uppercase with hyphens).
- Vultr uses the format `"ewr"` (lowercase airport code).
- Modal uses the format `"us-east"` (lowercase with hyphens, different taxonomy).
- AWS uses the format `"us-east-1"` (lowercase, hyphenated, numeric suffix — EC2 region codes).
- GCP is zonal: zone `"us-west1-a"` (region `"us-west1"` derived as the `<region>-<suffix>` prefix).
- imbue_cloud validates only `KNOWN_OVH_US_REGIONS` = `{"US-EAST-VA", "US-WEST-OR"}`, which is a subset of OVH regions.

All of these formats are untyped `str` with no shared enum.

### 2.4 Terminology Variants

| Term | Context |
|---|---|
| `default_region` (config field) | Modal, Vultr, OVH, AWS, GCP config classes |
| `default_zone` (config field) | GCP only (GCE VMs are zonal) |
| `region` (build arg key) | imbue_cloud `-b region=` |
| `--vultr-region=` / `--aws-region=` / `--ovh-region=` (alias `--ovh-datacenter=`) / `--gcp-zone=` (per-provider build arg) | Replaced the retired shared `--vps-region=` prefix; minds agent_creator passes `--vultr-region=` (VULTR) and `--aws-region=` (AWS) |
| `KNOWN_OVH_US_REGIONS` | frozenset constant for imbue_cloud validation |

### 2.5 Definition Ambiguities / Inconsistencies

- **Five incompatible region naming conventions** across providers: OVH (`"US-EAST-VA"`), Vultr (`"ewr"`), Modal (`"us-east"`), AWS (`"us-east-1"`), and GCP zones (`"us-west1-a"`).
- The imbue_cloud region validation (`KNOWN_OVH_US_REGIONS`) is implicitly OVH-specific but lives in `mngr_imbue_cloud/primitives.py`, which could mislead maintainers into thinking it applies broadly.
- Modal's `region` config is a provider-level default; imbue_cloud's `region` is a per-create build arg. This asymmetry is undocumented at the mngr level.

### 2.6 Doc/Code Divergences

None found. All config field descriptions accurately describe their purpose.

### 2.7 Recommended Canonical Term + Definition

There is no single canonical region concept; each provider has its own region semantics. To standardize, a `RegionName` `SafeName` subtype (or at minimum a provider-specific validated type) should be introduced per provider. The per-provider build args (`--vultr-region=`, `--aws-region=`, `--ovh-region=`, `--gcp-zone=`) live in separate namespaces by design after the `--vps-region=` retirement; this is now clearly documented per provider. The imbue_cloud `KNOWN_OVH_US_REGIONS` constant should be co-located with OVH config rather than imbue_cloud primitives.

---

## 3. Hosts

### 3.1 Canonical Definition

A **host** is a managed compute environment on which agents run. It is identified by a `HostId` and a `HostName`, owned by exactly one `ProviderInstance`, and exposed via two complementary runtime interfaces:

**HostInterface** (base, offline-capable) — `libs/mngr/imbue/mngr/interfaces/host.py:49`
```python
class HostInterface(MutableModel, ABC):
    id: HostId
    pre_baked_agent_id: AgentId | None
```

**OnlineHostInterface** (adds command execution, SSH, agent management) — `libs/mngr/imbue/mngr/interfaces/host.py:404`
```python
class OnlineHostInterface(HostInterface, OuterHostInterface, ABC):
    """Hosts that are online and accessible for operations."""
```

**OuterHostInterface** (the "outer machine" a container/sandbox runs on) — `libs/mngr/imbue/mngr/interfaces/host.py:288`
```python
class OuterHostInterface(HostFileReadInterface, HostFileWriteInterface, ABC):
    id: HostId
    connector: PyinfraConnector
```

**DiscoveredHost** (lightweight discovery record, before connecting) — `libs/mngr/imbue/mngr/primitives.py:549`
```python
class DiscoveredHost(FrozenModel):
    host_id: HostId
    host_name: HostName
    provider_name: ProviderInstanceName
    host_state: "HostState | None"
```

**HostDetails** (full listing-time data, returned by `get_host_and_agent_details`) — `libs/mngr/imbue/mngr/interfaces/data_types.py:490`

The concrete implementation is `Host` in `libs/mngr/imbue/mngr/hosts/host.py`.

### 3.2 Distinguishing Host vs. Provider vs. Agent

| Concept | Scope | Key property |
|---|---|---|
| **Provider (Instance)** | manages multiple hosts | One per config entry; creates/destroys hosts |
| **Host** | managed environment for agents | One per machine/container/VM; identified by `HostId` |
| **Agent** | process running on a host | Multiple per host; identified by `AgentId` |

The `HostInterface.host_dir` property points to a directory on the host where mngr stores host and agent metadata (data.json, activity files, etc.).

### 3.3 imbue_cloud Host Pool (special case)

imbue_cloud leases pre-baked VPS hosts from a centrally managed pool. The "pool hosts" are VPS machines (OVH-backed via mngr_ovh or Vultr-backed) that have been pre-provisioned with a Docker container (running the FCT image) and a pre-baked agent. A user's `mngr create` on imbue_cloud leases one of these pool hosts (via the connector HTTP API) rather than provisioning a new VPS.

```python
# ImbueCloudProvider.create_host → calls client.lease_host(...)
# libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/instance.py:1113 (lease_host at :1208 / :1291)
```

The pool is maintained server-side in a `pool_hosts` database table, not in client code. The `mngr imbue_cloud admin bake` command creates and registers pool hosts.

### 3.4 Terminology Variants

| Term | Type/Location | Notes |
|---|---|---|
| `host` | General term | Used throughout |
| `HostId` | `RandomId` subclass; prefix `"host"` | `primitives.py:287` |
| `HostName` | `SafeName` subclass; no dots allowed | `primitives.py:350`; dot is reserved for `HOST.PROVIDER` address syntax |
| `HostAddress` | parsed `HOST[.PROVIDER]` string | `primitives.py:375` |
| `HostInterface` | offline-capable interface | `interfaces/host.py:49` |
| `OnlineHostInterface` | full online interface | `interfaces/host.py:404` |
| `OuterHostInterface` | interface for the machine hosting a container | `interfaces/host.py:288` |
| `DiscoveredHost` | lightweight discovery struct | `primitives.py:549` |
| `HostDetails` | full listing data | `interfaces/data_types.py:490` |
| `CertifiedHostData` | JSON stored in host's `data.json` | `interfaces/data_types.py:265` |
| `pool host` | imbue_cloud term for pre-baked VPS | Not a type; used in comments/docstrings |

### 3.5 Definition Ambiguities / Inconsistencies

- `OuterHostInterface` models "the machine that hosts a container" (e.g. the VPS that runs the Docker container that IS the mngr host). The term "outer host" is intuitive for VPS-Docker providers but confusing for local/modal hosts which have no outer.
- `HostName` forbids dots (enforced at `primitives.py:350`; comment says "dot is reserved as separator in HOST.PROVIDER host addresses") but `HostId` starts with `"host-"` containing a hyphen — so the `HOST.PROVIDER` address separator convention is a dot while ID prefixes use hyphens.
- `CertifiedHostData.host_name` is typed `str` not `HostName`, meaning it can contain characters forbidden by `HostName`'s `SafeName` validation. This is intentional (backward compat with old data.json files that had values like `"@local"`), but creates a type mismatch.

### 3.6 Doc/Code Divergences

- `CertifiedHostData._handle_backwards_compatibility()` silently normalizes old `host_name` values (`"@local"`, `"local"`, `"unknown-host-at-local"`) to `"localhost"` (`interfaces/data_types.py:282–304`). No docs or changelog mention this normalization; it could surprise callers reading `certified_data.host_name` and expecting it to equal what was set at host creation.

### 3.7 Recommended Canonical Term + Definition

- **host**: a managed compute environment (local machine, VM, container, or sandbox) that belongs to exactly one provider instance, identified by `HostId` and `HostName`. Hosts can be online (`OnlineHostInterface`) or offline (`HostInterface`). The term "outer host" (`OuterHostInterface`) should be used only for the physical machine that hosts a container.
- `CertifiedHostData.host_name` should be typed `HostName` (or explicitly `str` with a validator that accepts legacy values). The backward-compat silent normalization should be documented.

---

## 4. Agents (the mngr agent primitive)

### 4.1 Canonical Definition

A **mngr agent** is a named, identified process (typically an AI coding assistant) that runs inside a tmux session on a host. At the mngr core level, an agent is defined by:

**AgentInterface** — `libs/mngr/imbue/mngr/interfaces/agent.py:39`
```python
class AgentInterface(MutableModel, ABC, Generic[AgentConfigT]):
    id: AgentId           # Unique identifier
    name: AgentName       # Human-readable name
    agent_type: AgentTypeName  # e.g., "claude", "codex"
    work_dir: Path        # Working directory
    create_time: datetime
    host_id: HostId
    mngr_ctx: MngrContext
    agent_config: AgentConfigT
```

Agents are persisted as `data.json` files inside a per-agent state directory on the host. The concrete implementation is `BaseAgent` in `libs/mngr/imbue/mngr/agents/base_agent.py:64`.

**AgentId** — `libs/mngr/imbue/mngr/primitives.py:281`
```python
class AgentId(RandomId):
    PREFIX = "agent"
```

**AgentName** — `libs/mngr/imbue/mngr/primitives.py:346`
```python
class AgentName(SafeName):
    """Human-readable name for an agent."""
```

**DiscoveredAgent** (lightweight discovery record) — `libs/mngr/imbue/mngr/primitives.py:560`
```python
class DiscoveredAgent(FrozenModel):
    host_id: HostId
    agent_id: AgentId
    agent_name: AgentName
    provider_name: ProviderInstanceName
    certified_data: Mapping[str, Any]  # raw data.json contents
```

**AgentDetails** (full listing data) — `libs/mngr/imbue/mngr/interfaces/data_types.py:534`

### 4.2 Labels (workspace= and is_primary)

Labels are arbitrary `dict[str, str]` key-value pairs attached to an agent and stored in `data.json`:

```python
# BaseAgent.get_labels() / set_labels()
# libs/mngr/imbue/mngr/agents/base_agent.py:172-178
def get_labels(self) -> dict[str, str]:
    data = self._read_data()
    return data.get("labels", {})
```

The labels `workspace` and `is_primary` are mngr **conventions** applied by Minds:

```python
# apps/minds/imbue/minds/desktop_client/agent_creator.py:616-630
"--label", f"workspace={host_name}",
"--label", "is_primary=true",
```

These labels are NOT enforced or typed by mngr core — they are plain strings. Minds' `backend_resolver.py` filters on them:
```python
# apps/minds/imbue/minds/desktop_client/backend_resolver.py:742
if "workspace" in agent.labels and "is_primary" in agent.labels
```

The `workspace` label value is set to the host name (not a boolean `"true"`), while `is_primary` is set to the string `"true"`.

Note: in some tests the `workspace` label value is `"true"` (`backend_resolver_test.py:315`) and in others it is the workspace name (`providers_panel_test.py:362`). See Section 4.5.

### 4.3 Agent Creation (mngr create / launch-task)

Agents are created via `mngr create` CLI, which calls `api/create.py`. The key steps are:

1. Resolve provider and host
2. Call `on_before_create` plugin hooks
3. Call `host.create_agent_work_dir()` (set up the working directory)
4. Call `host.create_agent_state()` (write `data.json`, create tmux session config)
5. Call `host.provision_agent()` (install packages, create config files)
6. Call `host.start_agents()` (launch the agent process)

There is no separate "launch-task" concept at the mngr core level; `mngr create` is the single entry point for creating agents.

### 4.4 Terminology Variants

| Term | Type/Location | Notes |
|---|---|---|
| `AgentId` | `RandomId` with prefix `"agent"` | `primitives.py:281` |
| `AgentName` | `SafeName` subclass | `primitives.py:346` |
| `AgentTypeName` | `SafeName` subclass | `primitives.py:496`; e.g., `"claude"`, `"codex"` |
| `AgentNameOrId` | union alias `AgentId | AgentName` | `primitives.py:362` |
| `AgentAddress` | parsed `NAME[@HOST[.PROVIDER]]` | `primitives.py:410` |
| `AgentInterface` | abstract base | `interfaces/agent.py:39` |
| `BaseAgent` | concrete implementation | `agents/base_agent.py:64` |
| `DiscoveredAgent` | lightweight discovery record | `primitives.py:560` |
| `AgentDetails` | full listing data | `interfaces/data_types.py:534` |
| `CreateAgentOptions` | options passed to `create_agent_state` | `interfaces/host.py:932` |
| `agent_type` | string like `"claude"` or `"codex"` | field on `AgentInterface` and `AgentDetails` |
| `workspace` label | plain string label, value = host name | convention only, in Minds |
| `is_primary` label | plain string label, value = `"true"` | convention only, in Minds |

### 4.5 Definition Ambiguities / Inconsistencies

- **`workspace` label value inconsistency**: In `agent_creator.py:617`, `workspace` is set to `{host_name}` (the actual workspace/host name string). But in `backend_resolver_test.py:315`, the test fixture uses `{"workspace": "true", "is_primary": "true"}`. The `backend_resolver.py` code only checks `"workspace" in agent.labels` (key presence, not value), so functionally it doesn't matter — but the inconsistent value in tests could confuse maintainers.
- **Agent type vs. agent implementation**: `agent_type` is a string field (`AgentTypeName`) on `AgentInterface`. The actual Python class implementing the agent is found via a separate registry (`config/agent_class_registry.py`). These two things — the string name and the class — are decoupled, which is correct but underdocumented.
- **`pre_baked_agent_id` on HostInterface** (`interfaces/host.py:53-65`): This field links a host to an agent that was already baked into the host image (imbue_cloud fast path). It is on `HostInterface` but is only non-None for `ImbueCloudHost`. This bleeds imbue_cloud-specific semantics into the core interface.

### 4.6 Doc/Code Divergences

- `DiscoveredAgent.certified_data` docstring says "certified_data field contains the raw data.json contents" but the actual `data.json` is the **agent-level** data, while the similarly named `CertifiedHostData` is the **host-level** data. The naming is reused but means different things at agent vs. host level.
- `BaseAgent.get_labels()` reads from `data["labels"]` (uncertified agent data), but `DiscoveredAgent.labels` also reads from `certified_data.get("labels", {})` (`primitives.py:627`). Both are the same source (agent's `data.json`), but this is not explicitly stated.

### 4.7 Recommended Canonical Term + Definition

- **agent**: a named, identified AI/automation process running in a tmux session on a host. Core identity: `(AgentId, AgentName, AgentTypeName, host_id)`. Labels are arbitrary key-value metadata; Minds uses `workspace=<host_name>` and `is_primary=true` to select the primary workspace agent.
- The `workspace` label should have a documented canonical value format (the host name string, not `"true"`). Tests should be corrected to use the host-name form.
- `pre_baked_agent_id` on `HostInterface` should be documented as imbue_cloud-only and considered for refactoring into the `ImbueCloudHost` subclass.

---

## 5. Lifecycle State

### 5.1 Canonical Definition

Lifecycle state is represented by **two enums** — one for hosts and one for agents — both defined in `libs/mngr/imbue/mngr/primitives.py`.

**HostState** — `libs/mngr/imbue/mngr/primitives.py:244`
```python
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
    UNKNOWN = auto()
```

**AgentLifecycleState** — `libs/mngr/imbue/mngr/primitives.py:263`
```python
class AgentLifecycleState(UpperCaseStrEnum):
    """The lifecycle state of an agent."""
    STOPPED = auto()
    RUNNING = auto()
    WAITING = auto()
    REPLACED = auto()
    RUNNING_UNKNOWN_AGENT_TYPE = auto()
    DONE = auto()
    UNKNOWN = auto()
```

`UpperCaseStrEnum` makes these uppercase-string-valued enums (e.g. `HostState.RUNNING` serializes as `"RUNNING"`).

### 5.2 All Usages

**HostState** is used in:
- `HostDetails.state: HostState | None` — `interfaces/data_types.py:506`
- `DiscoveredHost.host_state: "HostState | None"` — `primitives.py:555`
- `HostInterface.get_state() -> HostState` — `interfaces/host.py:183`
- Provider-specific state computation:
  - `_map_docker_status_to_host_state()` — `mngr_imbue_cloud/instance.py:257`
  - `_derive_host_state_from_raw()` — `mngr_imbue_cloud/instance.py:213`
- Listing/display: `libs/mngr/imbue/mngr/api/list.py` (consumed for UI badges)

**AgentLifecycleState** is used in:
- `AgentDetails.state: AgentLifecycleState` — `interfaces/data_types.py:559`
- `AgentInterface.get_lifecycle_state() -> AgentLifecycleState` — `interfaces/agent.py:141`
- `BaseAgent.get_lifecycle_state()` — `agents/base_agent.py`
- `determine_lifecycle_state()` pure function — `hosts/common.py:342` (determines state from tmux info + ps output)
- `build_agent_details_from_offline_ref()` hardcodes `AgentLifecycleState.STOPPED` for offline agents — `interfaces/provider_instance.py:246`

### 5.3 Idle Detection and Shutdown

The stop/destroy lifecycle is driven by idle detection:

**IdleMode** — `libs/mngr/imbue/mngr/primitives.py:80`
```python
class IdleMode(UpperCaseStrEnum):
    IO = auto(); USER = auto(); AGENT = auto(); SSH = auto()
    CREATE = auto(); BOOT = auto(); START = auto(); RUN = auto()
    CUSTOM = auto(); DISABLED = auto()
```

**ActivitySource** — `libs/mngr/imbue/mngr/primitives.py:122`
```python
class ActivitySource(UpperCaseStrEnum):
    CREATE = auto(); BOOT = auto(); START = auto()
    SSH = auto(); PROCESS = auto(); AGENT = auto(); USER = auto()
```

The mapping from `IdleMode` to `ActivitySource` sets is canonical at `interfaces/data_types.py:45` (`ACTIVITY_SOURCES_BY_IDLE_MODE`).

**Activity** is recorded by touching timestamp files under `host_dir/activity/<source>` (host-level) or `agent_state_dir/activity/<source>` (agent-level). Idle seconds is computed from the most recent activity timestamp across active sources.

**CleanupAction** — `libs/mngr/imbue/mngr/primitives.py:168`
```python
class CleanupAction(UpperCaseStrEnum):
    DESTROY = auto()
    STOP = auto()
```

### 5.4 Lifecycle Transitions (start/stop/destroy)

Core lifecycle methods on `ProviderInstanceInterface`:
- `create_host()` → produces an online host
- `stop_host()` → stops a running host (optionally creates snapshot)
- `start_host()` → starts a stopped host (optionally from snapshot)
- `destroy_host()` → permanently destroys a host; data wipe + lease release for imbue_cloud
- `delete_host()` → deletes all records for a destroyed host (called by GC after grace period)

Agent lifecycle:
- `host.start_agents(agent_ids)` → create tmux sessions + launch processes
- `host.stop_agents(agent_ids)` → gracefully terminate processes
- `host.destroy_agent(agent)` → remove agent state directory + work directory

`CertifiedHostData.stop_reason: str | None` (`interfaces/data_types.py:345`) records why a host was last stopped: `"PAUSED"` (idle), `"STOPPED"` (user requested or agents exited), or `None` (crashed).

### 5.5 Competing / Multiple Definitions

- `HostState.PAUSED` is used by Modal (sandbox paused via Modal API) and by the Docker provider (container paused). At the mngr level it means "host suspended but recoverable" but the recovery mechanism differs per provider.
- `HostState.UNAUTHENTICATED` is used in two distinct scenarios: (1) host's SSH key was rejected (`HostAuthenticationError`), and (2) a Docker container is running but inner SSH was unreachable. Both use the same state for semantically different failure modes.
- `AgentLifecycleState.DONE` vs `STOPPED`: `DONE` means the agent's tmux pane process exited naturally (pane is dead or shell prompt returned); `STOPPED` means the tmux session doesn't exist. But `build_agent_details_from_offline_ref()` hardcodes `STOPPED` for all offline agents regardless of their actual prior state, losing fidelity.

### 5.6 Terminology Variants

| Term | Type/Location | Notes |
|---|---|---|
| `HostState` | `UpperCaseStrEnum` | Host lifecycle state |
| `AgentLifecycleState` | `UpperCaseStrEnum` | Agent lifecycle state |
| `state` (field name) | `HostDetails.state`, `AgentDetails.state` | Both are optional in data structures |
| `lifecycle state` | Used in docstrings | Colloquial term for `AgentLifecycleState` |
| `container status badges` | UI term | Maps to `HostState` values |
| `idle_mode` | `IdleMode` enum | Controls when a host is considered idle |
| `idle_seconds` | `float | None` | Computed from last activity timestamp |
| `stop_reason` | `str | None` in `CertifiedHostData` | Untyped string (`"PAUSED"`, `"STOPPED"`, or None) |
| `failure_reason` | `str | None` in `CertifiedHostData`, `HostDetails` | Why a host failed during creation |

### 5.7 Definition Ambiguities / Inconsistencies

- **`HostState.UNKNOWN`** vs **`HostDetails.state: HostState | None`**: The `UNKNOWN` enum value means "provider discovery failed transiently" (`primitives.py:256-259`), while `None` on `HostDetails.state` means "not observed / not applicable". These two "unknown" representations are semantically different but easily confused.
- **`stop_reason` is untyped `str | None`** while the rest of the lifecycle state is typed. The comment says the values are `"PAUSED"`, `"STOPPED"`, or `None` — these should be an enum or at least `Literal` typed.
- **`AgentLifecycleState` has no CREATED/CREATING state**: the `mngr create` operation is in-progress before the agent is in `STOPPED` or `RUNNING` state. During creation, the agent is not discoverable, so there is no state for "currently being provisioned".
- **`RUNNING_UNKNOWN_AGENT_TYPE`** is a catch-all for "agent type unknown but process running." It is a valid state but conflates two conditions: the process IS running (RUNNING), but we cannot verify it's the right process.

### 5.8 Doc/Code Divergences

- `CertifiedHostData.stop_reason` docstring says `"PAUSED" (idle), "STOPPED" (user requested or all agents exited), or None (crashed)`. But the code in `hosts/host.py` that writes this field may use different string values — this docstring is the only specification. **This is a DOC/CODE DIVERGENCE risk**: if the writer uses a different string (e.g. `"paused"` lowercase), the docstring contract is violated with no type check to catch it.

### 5.9 Recommended Canonical Term + Definition

- **HostState**: enum with values BUILDING, STARTING, RUNNING, STOPPING, STOPPED, PAUSED, CRASHED, FAILED, DESTROYED, UNAUTHENTICATED, UNKNOWN.
- **AgentLifecycleState**: enum with values STOPPED, RUNNING, WAITING, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE, DONE, UNKNOWN.
- `stop_reason` should be typed as `Literal["PAUSED", "STOPPED"] | None` or converted to an enum.
- `HostState.UNKNOWN` (transient provider error) and `HostDetails.state = None` (not applicable) should be documented explicitly as different sentinel values in a single place.
- Consider adding `CREATING` to `AgentLifecycleState` for agents in-progress during `mngr create`.

---

## Cross-Cutting Inconsistencies and Ambiguities (Summary)

The following are the most important findings that span multiple concepts:

1. **Provider backend vs. instance naming confusion**: The two-level provider abstraction (backend = factory, instance = configured endpoint) is architecturally sound but the term "provider" is used interchangeably in docs and comments for both levels. `VultrProvider` is named like an instance but plays both roles.

2. **No shared region type**: Five incompatible region string formats (`"US-EAST-VA"`, `"ewr"`, `"us-east"`, `"us-east-1"`, and GCP zone `"us-west1-a"`) exist across OVH, Vultr, Modal, AWS, and GCP with no shared validation or enum. The imbue_cloud `KNOWN_OVH_US_REGIONS` constant is OVH-specific but lives in the imbue_cloud package, not the OVH package.

3. **`workspace` label value inconsistency**: Minds sets `workspace=<host_name>` in production but some tests use `workspace="true"`. The filtering logic (`"workspace" in agent.labels`) passes either way, hiding a documentation-level bug.

4. **`HostState.UNKNOWN` vs. `HostDetails.state = None`**: Two different sentinel meanings for "unknown host state" exist — one as an enum value (provider error) and one as `None` on a field (not applicable). They are incomparable and underdocumented.

5. **`stop_reason` is an untyped string**: The lifecycle "why was this host stopped" signal is a plain `str | None` on `CertifiedHostData`, not a typed enum, creating a silent correctness risk.

6. **`pre_baked_agent_id` bleeds imbue_cloud semantics into `HostInterface`**: A field that is only meaningful for one provider (imbue_cloud fast-path adoption) lives on the shared base interface rather than on the subclass.

7. **`CertifiedHostData.host_name` type mismatch**: Typed as `str` (not `HostName`) to accommodate legacy values, but silent normalization of old values in `_handle_backwards_compatibility` is nowhere documented.
