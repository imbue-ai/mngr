# Group 3: compute & runtime

---

## 1. Workspaces / Minds

### Canonical Definition

A **workspace** is a persistent agent container created by the desktop client (`minds`). In code it is the pair of:
1. A **host** (Docker container / Lima VM / Vultr VPS / OVH VPS / imbue_cloud-leased machine) identified by a `HostId`.
2. A **primary mngr agent** (the `system-services` agent, `agent_type = main`) running on that host, labelled `workspace=<host_name>` and `is_primary=true`.

The desktop client discovers workspaces by filtering the mngr discovery stream for agents bearing **both** `workspace` and `is_primary` labels.

- `apps/minds/imbue/minds/desktop_client/backend_resolver.py:711-722` — `list_known_workspace_ids` filters `DiscoveredAgent` records for `"workspace" in agent.labels and "is_primary" in agent.labels`.
- `apps/minds/imbue/minds/desktop_client/agent_creator.py:549,558,563` — `_build_mngr_create_command` appends `--label workspace={host_name}` and `--label is_primary=true`.
- `.external_worktrees/forever-claude-template/.mngr/settings.toml:113` — `[agent_types.main]` is the services-agent type; `command = "sleep infinity && claude"` keeps window 0 dormant (bootstrap runs separately).

### "Mind" in Code

`mind` is **not a code-level type or class**. It appears exclusively in:
- **UI surface strings** and **function names** in the desktop client that expose liveness state to the landing page:
  - `apps/minds/imbue/minds/desktop_client/mind_liveness.py:55` — `class MindLiveness(UpperCaseStrEnum)` with values `RUNNING / STOPPED / UNKNOWN`.
  - `apps/minds/imbue/minds/desktop_client/mind_liveness.py:114` — `compute_mind_liveness_by_agent_id(...)`.
  - `apps/minds/imbue/minds/desktop_client/templates.py:198` — `mind_liveness_by_agent_id` kwarg to `render_landing_page`.
- The **package name** `imbue.minds` and **app name** `minds` (user-facing product).

There is no Python class named `Mind`. The code object is an `AgentId` (the system-services agent's id). "Mind" in UI copy is a synonym for "workspace".

### All Usages

| Usage | Location |
|---|---|
| `MindLiveness` enum (RUNNING/STOPPED/UNKNOWN) | `mind_liveness.py:55` |
| `compute_mind_liveness_by_agent_id` | `mind_liveness.py:114` |
| `get_shutdown_capable_workspace_agent_ids` | `mind_liveness.py:95` |
| `mind_liveness_by_agent_id` render kwarg | `templates.py:198` |
| workspace label filtering in `list_known_workspace_ids` | `backend_resolver.py:711` |
| workspace label filtering in `list_active_workspace_ids` | `backend_resolver.py:724` |
| `--label workspace={host_name}` at create time | `agent_creator.py:549` |
| `--label is_primary=true` at create time | `agent_creator.py:563` |

### Competing/Multiple Definitions

- `"workspace"` in `.mngr/settings.toml:61` (`[commands.list] include__extend = ["has(labels.workspace)"]`) uses the label to filter the list to workspace agents only — consistent with the desktop client's definition.
- `"workspace"` elsewhere in code can refer loosely to the whole container environment (e.g. `AgentCreationStatus.CREATING_WORKSPACE`, `"Waiting for workspace to be ready"`), the host's git work directory, or the `WorkspacePaths` config object.
- `WorkspacePaths` (`apps/minds/imbue/minds/config/data_types.py`) is a config/filesystem abstraction, not a workspace as defined above.

### Terminology Variants

| Term | Where used |
|---|---|
| workspace | code-canonical (labels, function names, UI) |
| mind | UI strings, function names in desktop client |
| agent | mngr-internal; each workspace has 2+ agents (system-services + chat agent(s)) |
| container | infrastructure layer |

### Ambiguities / Inconsistencies

1. `list_known_workspace_ids` returns the **system-services** agent id (the `is_primary=true` agent), not the chat agent id. Yet users think of "the workspace" as the chat session. The primary agent id is the workspace's canonical identifier even though it runs `sleep infinity`.
2. The term "mind" is used in `mind_liveness.py` but never defined as a type. Code comments explain "container liveness of a mind" without grounding it in a class.
3. `LaunchMode` in `primitives.py:35` says `DOCKER / CLOUD / LIMA / IMBUE_CLOUD` — these are compute providers, not the workspace itself, but are documented as "How a workspace agent should be launched".
4. `SYSTEM_SERVICES_AGENT_NAME = "system-services"` (`backend_resolver.py:37`) is the agent name inside every workspace host, but minds' create command builds the address as `system-services@{host_name}.{provider}` — so the host name is the workspace identity.

### DOC/CODE DIVERGENCES

- `concepts doc:30` says "labeled workspace=" implying the label value is truthy. Code at `backend_resolver.py:721` checks `"workspace" in agent.labels` (key presence, not value), but `agent_creator.py:549` sets `workspace={host_name}` (value = host name string). The label check is purely for presence, not the string "true". No actual divergence, but the doc is imprecise.

### Recommended Canonical Term

**workspace** (primary) with **mind** as acceptable UI/product synonym.

Rationale: "workspace" is the code term used in labels, function names, and architecture docs. "mind" is the product-level brand name (the app is `minds`). The recommended definition: *a workspace is the combination of a persistent mngr host and the `system-services` (primary) agent running on it, identified at the API level by that agent's `AgentId`*.

---

## 2. Templates

### Canonical Definition

Templates are two distinct things that share the name:

**2a. `mngr create` templates** (settings-level): Named presets of `mngr create` CLI arguments defined under `[create_templates.<name>]` in a repo's `.mngr/settings.toml`. Applied to a create invocation via `--template <name>`. Multiple templates can be stacked in order.

- `libs/mngr/imbue/mngr/cli/common_opts.py:739` — `apply_create_template(...)` applies each named template's options to the parameter dict.
- `.external_worktrees/forever-claude-template/.mngr/settings.toml` — declares `main`, `docker`, `lima`, `vultr`, `ovh`, `imbue_cloud`, `chat`, `worktree`, `worker`, `crystallize-worker` templates.

**2b. The forever-claude-template (FCT)**: The git repository at `.external_worktrees/forever-claude-template/` that serves as the default workspace source code. It is the template *repo* that gets cloned into workspace containers.

- `apps/minds/imbue/minds/desktop_client/templates.py:245` — `_FALLBACK_GIT_URL = "https://github.com/imbue-ai/forever-claude-template.git"` is the default create-form git URL.

### Template Stacking (2a)

Template stacking means passing multiple `--template` flags: `--template main --template docker`. The `apply_create_template` function iterates `template_names` in order and applies each template's options to the params accumulator.

- `libs/mngr/imbue/mngr/cli/common_opts.py:769-838` — the loop `for template_name in template_names`.
- Merge semantics per template:
  - **Scalar fields**: latest-wins; CLI-provided scalars always override template values (checked via `ParameterSource.DEFAULT`).
  - **Aggregate fields** (list/tuple/dict/set): assign-by-default, guarded by narrowing check. Use `key__extend` in the template definition to opt into additive concatenation.
- `apps/minds/imbue/minds/desktop_client/agent_creator.py:591,611` — minds always passes `["--template", "main", "--template", "<provider_mode>"]` for DOCKER/LIMA/CLOUD/IMBUE_CLOUD modes.

### All `create_templates` Defined in FCT `.mngr/settings.toml`

| Template name | Purpose |
|---|---|
| `main` | Shared defaults across all provider modes (agent type = main, extra_window for bootstrap/telegram/terminal/reviewer/git_auth) |
| `docker` | Docker provider; sets `build_arg`, `start_arg`, `idle_mode`, `pass_host_env`, `post_host_create_command` (fct-seed) |
| `lima` | Lima VM provider; sets `extra_provision_command` for setup/install/build scripts |
| `vultr` | Vultr VPS provider; adds region/plan build args |
| `ovh` | OVH VPS provider (imbue-cloud pool bake) |
| `imbue_cloud` | imbue_cloud-leased pool hosts; disable idle, pass LiteLLM creds, fct-seed |
| `chat` | In-workspace chat agent (type = claude, no new host) |
| `worktree` | In-workspace worktree agent (type = claude, in a new git worktree) |
| `worker` | General purpose background agent (type = worker, reviewer enabled) |
| `crystallize-worker` | Crystallize/heal-skill lifecycle worker (type = worker, extra provision for worker sub-skills) |

### Relationship Between 2a and 2b

The FCT (2b) is just a git repo with its own `.mngr/settings.toml` that declares (2a) templates. The templates inside FCT are resolved by a local `.mngr/settings.toml` when `mngr create` is run from the FCT checkout directory (or when `workspace_dir` points to a FCT clone).

### Ambiguities / Inconsistencies

1. The word "template" in "the minds desktop client clones the forever-claude-template" (2b) and in `--template main` (2a) is the same word for unrelated concepts.
2. `[create_templates.imbue_cloud]` (`settings.toml:326`) sets `target_path` and `build_arg__extend` for the SLOW path but notes they are ignored by the FAST adopt path — this asymmetry is commented but not structurally enforced.
3. `[agent_types.main]` and `[create_templates.main]` are separate TOML sections; `create_templates.main` sets `type = "main"` which references `agent_types.main`. The naming overlap ("main" as both a create-template and an agent-type) is confusing.

### Recommended Canonical Terms

- **create template** (for 2a): a named preset of `mngr create` parameters in `.mngr/settings.toml`.
- **template repository** or **FCT** (for 2b): the git repo cloned to form a workspace.
- **template stacking**: the ordered application of multiple `--template` flags.

---

## 3. Services

### Canonical Definition

A **service** is an entry in `services.toml` under `[services.<name>]`. Each service has:
- `command`: shell command run in its own tmux window (`svc-<name>`).
- `restart`: restart policy (string enum).

The bootstrap service manager (`libs/bootstrap/src/bootstrap/manager.py`) reconciles the set of `svc-*` tmux windows to match the current `services.toml` on every POLL_INTERVAL (5 seconds) or on file mtime change.

- `.external_worktrees/forever-claude-template/services.toml` — canonical example with 7 services.
- `libs/bootstrap/src/bootstrap/manager.py:44-58` — constants: `SERVICES_FILE = Path("services.toml")`, `SVC_PREFIX = "svc-"`, `DEFAULT_RESTART_POLICY = "never"`, `VALID_RESTART_POLICIES = frozenset({"never", "on-failure"})`.

### Service Schema

```toml
[services.<name>]
command = "<shell command>"
restart = "never" | "on-failure"   # default: "never" if absent
```

- `manager.py:502-525` — `_load_services()` returns `{name: {"command": str, "restart": str}}`.
- `manager.py:528-548` — `_normalize_restart_policy`: absent restart → `DEFAULT_RESTART_POLICY = "never"`. Unrecognized → warn and fall back to `"never"`.

### Restart Policy Enum

| Value | Behavior |
|---|---|
| `"never"` | Default. Exited service stays dead. |
| `"on-failure"` | Restart when exit status is non-zero. A clean exit (status 0) is left alone. |

- `manager.py:676-692` — `_compute_restarts`: only restarts when `restart == "on-failure"` AND `status != "0"`.

### Reconciliation Mechanism

1. On mtime change or startup: `_load_services()` → `_compute_actions(desired, current)` → stop removed/changed services, start new/changed ones.
2. Each poll (every 5s): `_list_exited_services()` → `_compute_restarts()` → `_restart_service()` for services matching `on-failure`.
3. Service window lifecycle:
   - Created via `tmux new-window -n svc-<name>`.
   - Command + exit-status recorder are sent via `tmux send-keys`: `<command>; tmux set-option -t <target> -w @svc_exit_status "$?"`.
   - Manager polls `@svc_exit_status` option to detect exited services (the window stays open at an idle shell after exit).

### Services in FCT `services.toml`

| Service | Restart | Purpose |
|---|---|---|
| `system_interface` | `on-failure` | Runs `forward_port.py` + `system-interface` web server |
| `web` | `on-failure` | Runs `forward_port.py` + `uv run web-server` |
| `cloudflared` | `on-failure` | Cloudflare tunnel |
| `app-watcher` | `on-failure` | Watches `runtime/applications.toml` and emits service events |
| `runtime-backup` | `on-failure` | Commits+pushes `runtime/` to `mindsbackup/$MNGR_AGENT_ID` branch |
| `host-backup` | `on-failure` | Restic-based full host backup to R2 |
| `deferred-install` | (no restart field set → "never") | One-time install of heavy packages (Playwright+Chromium) |

### Ambiguities / Inconsistencies

1. `deferred-install` has no `restart` key and thus defaults to `"never"` — intentional (it is idempotent via marker files, but restarts would be pointless). The TOML comment says "Idempotently installs deferred packages on first container boot".
2. The `app-watcher` service writes to `events/services/events.jsonl`; its events are named `service_registered` / `service_deregistered` — but these are really **application** registration events (see concept 4), not service events. There is a concept-naming collision between "service" (services.toml entry) and "application" (services that expose ports).

### Recommended Canonical Term + Definition

**service**: a named background process declared in `services.toml`, managed by the bootstrap service manager in a tmux window named `svc-<name>`, with a restart policy of `"never"` or `"on-failure"`.

---

## 4. Applications / Ports

### Canonical Definition

An **application** is a service that has registered a local URL by running `forward_port.py`. Applications are stored in `runtime/applications.toml` as an array of `{name, url}` entries. The `app-watcher` service monitors this file and emits `service_registered` / `service_deregistered` events to `events/services/events.jsonl`.

- `scripts/forward_port.py:1-134` (FCT) — the registration script. Usage: `python3 scripts/forward_port.py --name <name> --url http://localhost:<port>`.
- `apps/system_interface/` — the system_interface service itself calls `forward_port.py` as part of its startup command in `services.toml:6`.

### Application Schema

`runtime/applications.toml`:
```toml
[[applications]]
name = "system_interface"
url  = "http://localhost:8000"

[[applications]]
name = "web"
url  = "http://localhost:8080"
```

- `scripts/forward_port.py:20` — `DEFAULT_APPLICATIONS_FILE = "runtime/applications.toml"`.
- `scripts/forward_port.py:62-78` — `_upsert`: upserts by name, atomic write via temp file + `os.replace`.
- `libs/app_watcher/src/app_watcher/watcher.py:31` — `APPLICATIONS_FILE = Path("runtime/applications.toml")`.

### Relationship Between Service and Application

A **service** (services.toml entry) can optionally become an **application** by running `forward_port.py --name <name> --url <url>` on startup. This is done in the service's `command` field before the real service command:

```toml
[services.system_interface]
command = "python3 scripts/forward_port.py --url http://localhost:8000 --name system_interface && system-interface"
```

- FCT `services.toml:6` — the `&&` ensures `forward_port.py` registers before the service binary starts; if the service binary dies, the whole command exits and the `restart = "on-failure"` policy kicks in, re-running `forward_port.py` on the next start.
- Not all services are applications: `cloudflared`, `runtime-backup`, `host-backup`, and `deferred-install` do not call `forward_port.py`.

### How the Desktop Client Discovers Applications

1. `app-watcher` emits `service_registered` events to `events/services/events.jsonl` (in `MNGR_AGENT_STATE_DIR/events/services/`).
2. The `mngr observe` event stream consumed by `MngrCliBackendResolver` reads these events.
3. `BackendResolverInterface.get_backend_url(agent_id, service_name)` returns the inner URL.
4. The `mngr forward` plugin proxies traffic from the desktop to the inner URL using agent-subdomain routing (`{agent_id}.localhost`).

### Global vs. Local URLs

- **Local URL**: the inner `http://localhost:<port>` registered in `applications.toml`.
- **Global URL**: a Cloudflare tunnel URL, if a tunnel is configured. This is handled by the `cloudflared` service and the sharing system (separate from the application registration mechanism).

The desktop client accesses applications through the `mngr forward` plugin using the local URL; external access requires a tunnel.

### Naming Inconsistency

`app-watcher` emits events of type `"service_registered"` / `"service_deregistered"` for **application** registrations. The `ServiceRegisteredEvent` and `ServiceDeregisteredEvent` classes in `app_watcher/watcher.py:43,50` use the word "service" for what the TOML file calls "application". This is a significant naming collision:

- `watcher.py:39-40` — `_EVENT_TYPE_REGISTERED = EventType("service_registered")`, `_EVENT_TYPE_DEREGISTERED = EventType("service_deregistered")`.
- `backend_resolver.py:30` — `SERVICES_EVENT_SOURCE_NAME: Final[str] = "services"`.
- `backend_resolver.py:64` — `ServiceLogRecord.service` field = the application name.

DOC/CODE DIVERGENCE: the concepts doc calls this concept "applications / ports" but the event types and log records all use "service" terminology. The `services.toml` "service" and the `applications.toml` "application" are distinct concepts in the data model but conflated in event naming.

### Recommended Canonical Terms

- **service**: a `services.toml` entry (tmux process managed by bootstrap).
- **application**: a service that has registered a port via `forward_port.py` in `runtime/applications.toml`.
- `service_registered` / `service_deregistered` event types should ideally be renamed `application_registered` / `application_deregistered` to eliminate the collision.

---

## 5. Dependencies / Deferred Installs

### Canonical Definition

**Deferred installs** are packages too heavy to bake into the Docker image that are installed on first container boot by the `deferred-install` service (a `services.toml` entry). The contract uses per-package marker files under `/var/lib/minds/deferred-install/done.<package>` to gate idempotent installation.

- `scripts/deferred_install.sh` (FCT) — the implementation. `MARKER_DIR=/var/lib/minds/deferred-install`.
- `services.toml:43-46` (FCT) — the service entry: `command = "bash scripts/deferred_install.sh"` with no `restart` (defaults to `"never"`).

### Deferral Contract

1. Service runs `deferred_install.sh` on every boot.
2. Each package function (`_install_playwright`) checks its marker file (`done.playwright`); if present, it is a no-op.
3. If the package installs successfully, the marker is written. If it fails, the marker is NOT written, so the next boot retries.
4. A killed mid-install (e.g. pool bake's `mngr stop` during apt) is handled by `_recover_interrupted_dpkg()` which runs `dpkg --configure -a` before apt.

Currently the only deferred package is **Playwright + Chromium** (`_install_playwright`).

- `deferred_install.sh:46-68` — `_install_playwright`: runs `uv run playwright install --with-deps chromium` from `$REPO_ROOT=/mngr/code`.
- `deferred_install.sh:36-44` — `_recover_interrupted_dpkg`: handles pool host scenario.

### Vendored Dependencies

**`vendor/mngr`** (in FCT):
- Contains a vendored copy of the mngr platform libs (path-dependency in `pyproject.toml` via `[tool.uv.sources]`).
- `Dockerfile:57-63` (FCT) — copies `vendor/mngr/libs/*/pyproject.toml` manifests for uv workspace resolution.
- The FCT workspace uses `vendor/mngr/libs/{imbue_common,mngr,mngr_claude,mngr_modal,mngr_wait,resource_guards,concurrency_group}` as path dependencies.

**`vendor/tk`** (in FCT):
- A vendored copy of the `tk` ticket-tracking CLI used inside agent containers.
- Referenced in CLAUDE.md but not in Dockerfile explicitly (it is in the repo and installed as part of the workspace).

### Dockerfile Dependency Install Layers

The Dockerfile uses a two-stage approach to maximize Docker layer caching:

1. **Pre-COPY manifest layer** (`Dockerfile:38-71`): copies only `pyproject.toml` + `uv.lock` + npm manifests, then runs `scripts/install_dependencies.sh` to pre-warm the venv. This layer caches against dependency-manifest changes only.
2. **Source COPY layer** (`Dockerfile:77`): `COPY . /mngr/code/`. Source changes land here without invalidating the expensive dep layer.
3. **Build layer** (`Dockerfile:80`): `bash /mngr/code/scripts/build_workspace.sh` (npm build, etc.).
4. **Relocation layer** (`Dockerfile:88`): `mv /mngr/code /docker_build_code` — moves baked workspace off the volume mount path so it can be seeded at first boot.

### Ambiguities / Inconsistencies

1. `deferred-install` service has no `restart` policy → defaults to `"never"`. This means if it fails partway (e.g. apt network timeout), it will NOT retry automatically — the user must restart it manually or wait for the next container restart. The marker-file approach works around this by leaving a retry path on reboot, but there is no in-session retry.
2. The concepts doc mentions "vendored deps (vendor/mngr, vendor/tk)" — vendor/tk is present but vendor/mngr is the heavyweight one referenced in pyproject.toml sources.

### Recommended Canonical Term + Definition

**deferred install**: a package installed idempotently on first container boot (not in the Docker image), gated by a marker file at `/var/lib/minds/deferred-install/done.<package>`, run by the `deferred-install` service.

---

## 6. Health / Liveness

### Overview

There are **four distinct health/liveness mechanisms** in the codebase. They answer different questions, operate at different layers, and are driven by different actors.

---

### 6a. Mind Liveness (`MindLiveness`)

**What it checks**: Whether the workspace's Docker container / Lima VM is running, stopped, or unknown — at the **host/container lifecycle** level.

**Source of truth**: The mngr discovery snapshot's `HostState` (from `mngr observe --discovery-only`).

**Location**: `apps/minds/imbue/minds/desktop_client/mind_liveness.py`

**Enum**:
```python
class MindLiveness(UpperCaseStrEnum):  # mind_liveness.py:55
    RUNNING = auto()
    STOPPED = auto()
    UNKNOWN = auto()
```

**Key functions**:
- `classify_host_state(host_state: HostState | None) -> MindLiveness` (`mind_liveness.py:73`): maps `HostState.RUNNING` → `RUNNING`; `{STOPPED, STOPPING, CRASHED, FAILED}` → `STOPPED`; everything else (including `None`) → `UNKNOWN`.
- `compute_mind_liveness_by_agent_id(backend_resolver) -> dict[str, MindLiveness]` (`mind_liveness.py:114`): returns liveness for every shutdown-capable workspace.

**Scope**: Only applies to workspaces on "shutdown-capable" providers (`docker` and `lima`). Remote providers (Modal, OVH, Vultr, imbue_cloud) do not expose host shutdown to minds, so their workspaces are not tracked.

- `mind_liveness.py:46` — `_SHUTDOWN_CAPABLE_PROVIDER_BACKENDS: Final[frozenset[str]] = frozenset({"docker", "lima"})`.

**UI use**: Landing page Start/Stop controls and quit-time shutdown prompt.

---

### 6b. System Interface Health (`AgentHealth` / `SystemInterfaceHealthTracker`)

**What it checks**: Whether the `system_interface` web server **inside** the container is responding to HTTP requests.

**Source of truth**: Active HTTP probes through the `mngr forward` plugin, supplemented by failure envelopes emitted by the plugin.

**Location**: `apps/minds/imbue/minds/desktop_client/system_interface_health.py`

**Enum**:
```python
class AgentHealth(str, Enum):  # system_interface_health.py:80
    HEALTHY = "healthy"
    STUCK = "stuck"
    RESTARTING = "restarting"
    RESTART_FAILED = "restart_failed"
```

**State Machine**:
- `HEALTHY` → `STUCK`: background probe loop observes unbroken probe failures for ≥ `stuck_threshold_seconds` (default 5s).
- `STUCK` → `RESTARTING`: restart endpoint fires.
- `RESTARTING` → `RESTART_FAILED`: restart tier fails within its window.
- `{STUCK, RESTARTING, RESTART_FAILED}` → `HEALTHY`: any successful probe.

**Key classes**:
- `SystemInterfaceHealthTracker` (`system_interface_health.py:121`): the singleton state machine. Methods: `record_failure`, `record_probe_success`, `record_probe_failure`, `mark_restarting`, `mark_restart_failed`, `mark_stuck`.
- `_AgentRecord` (`system_interface_health.py:93`): per-agent mutable state (health, is_suspect, failure_run_started_at, last_restart_error).

**Trigger mechanism**: The `mngr forward` plugin emits a `system_interface_backend_failure` envelope when it observes a connection failure, mid-SSE EOF, or non-2xx response. `should_enroll_suspect_for_backend_failure(status_code)` (`system_interface_health.py:66`) acts on `None` (connection failure) and `{502, 503, 504}` statuses — it ignores app-level errors (4xx, 500).

**Probe mechanism**: `probe_workspace_through_plugin(mngr_forward_port, preauth_cookie, agent_id, ...)` (`agent_creator.py:128`): HTTP GET to `/{agent_id}.localhost:{forward_port}/` with preauth cookie.

**UI use**: Chrome titlebar; when STUCK → navigates content view to the recovery page.

---

### 6c. Recovery Probe (`DispatchTier` / `HostHealthResponse`)

**What it checks**: Batched in-container diagnostics to classify **why** a workspace is unresponsive and determine the appropriate recovery action tier.

**Location**: `apps/minds/imbue/minds/desktop_client/recovery_probe.py`

**Trigger**: The recovery page calls `GET /api/agents/{agent_id}/host-health` when `AgentHealth` is `STUCK` or `RESTART_FAILED`.

**Mechanism**: The endpoint runs `mngr exec <services_agent_id> <probe_command> --no-start --quiet`. The probe command base64-encodes a Python script sent via `echo ... | base64 -d | python3`. The sentinel `===PROBE-READY===` confirms the exec reached the container.

**7 probes** (in order):
1. Container running? — `host.state` from `mngr list --format json`.
2. System-services agent registered? — lifecycle state from `mngr list`.
3. Can exec into container? — sentinel presence from `mngr exec`.
4. Does `services.toml` declare `[services.system_interface]`? — tomllib parse inside container.
5. Is anything listening on the system-interface inner port? — `/proc/net/tcp` scan inside container.
6. Does the inner web server answer GET /? — curl inside container.
7. Has the system interface registered with the plugin resolver? — plugin resolver snapshot in minds.

**Dispatch tier classification** (`recovery_probe.py:575-602`):
```python
class DispatchTier(str, Enum):
    INTERFACE_UNRESPONSIVE = "interface_unresponsive"  # in-place agent restart
    HOST_OFFLINE = "host_offline"                       # unattended host restart
    HOST_UNRESPONSIVE = "host_unresponsive"             # require user consent
    WORKSPACE_MISCONFIGURED = "workspace_misconfigured" # services.toml missing [system_interface]
```

**Precedence** (checked in order): `WORKSPACE_MISCONFIGURED` beats all; `HOST_OFFLINE` when container is offline; `INTERFACE_UNRESPONSIVE` when container running + exec works; `HOST_UNRESPONSIVE` otherwise.

---

### 6d. Workspace Readiness Probe (Creation-time)

**What it checks**: Whether the `system_interface` web server is up and ready to serve after workspace creation.

**Location**: `apps/minds/imbue/minds/desktop_client/agent_creator.py`

**Mechanism**: `probe_workspace_through_plugin(mngr_forward_port, preauth_cookie, agent_id, ...)` — same HTTP probe as 6b but called during creation, waiting up to `workspace_ready_timeout_seconds` (default 300s) with `workspace_ready_poll_interval_seconds` (default 0.5s) between attempts.

- `agent_creator.py:1054-1065` — `workspace_ready_timeout_seconds = 300.0`, `workspace_ready_poll_interval_seconds = 0.5`, `workspace_ready_probe_timeout_seconds = 2.0`.
- On success: calls `system_interface_health_tracker.record_probe_success(agent_id)` to clear any probe-failure run accumulated during the warmup window (preventing a false STUCK transition right after creation).

---

### Summary Table: All Health/Liveness Mechanisms

| Mechanism | Class/Enum | What It Checks | Layer | Driven By |
|---|---|---|---|---|
| Mind Liveness | `MindLiveness` | Container up/down (HostState) | Host/container lifecycle | mngr discovery snapshot |
| System Interface Health | `AgentHealth` + `SystemInterfaceHealthTracker` | system_interface HTTP responding | Application (in-container web server) | mngr_forward failure envelopes + active probe loop |
| Recovery Probe | `DispatchTier` + `HostHealthResponse` | Why workspace is broken (7 in-container checks) | Full stack (host → container → service → port) | Recovery page on demand |
| Workspace Readiness | (no named type) | system_interface ready after creation | Application (in-container web server) | Creation flow (blocking wait) |

### Ambiguities / Inconsistencies

1. `_OFFLINE_HOST_STATES` is defined identically in two places:
   - `mind_liveness.py:50`: `frozenset({HostState.STOPPED, HostState.STOPPING, HostState.CRASHED, HostState.FAILED})` — typed as `frozenset[HostState]`.
   - `recovery_probe.py:270`: `frozenset({"STOPPED", "STOPPING", "CRASHED", "FAILED"})` — typed as `frozenset[str]` (string comparison, not HostState enum). This is a duplication risk.

2. The `AgentHealth` enum uses `str, Enum` (plain strings `"healthy"`, `"stuck"`, etc.) while `MindLiveness` uses `UpperCaseStrEnum` (values `"RUNNING"`, `"STOPPED"`, `"UNKNOWN"`). Inconsistent enum base style.

3. `STUCK` in `AgentHealth` refers to system_interface not responding; `STOPPED` in `MindLiveness` refers to the container being offline. These are distinct conditions but could be confused in a UI context where both mean "the workspace is not working."

4. The probe loop deliberately **excludes** `RESTARTING` agents from background probing (`snapshot_probe_targets` at `system_interface_health.py:341-366`) to avoid prematurely clearing the tracker. This is a subtle correctness constraint documented in comments but not enforced by the type system.

### Recommended Canonical Terms

- **mind liveness**: container-level up/down state (RUNNING/STOPPED/UNKNOWN). Term: `MindLiveness`.
- **system interface health**: application-level responsiveness state machine. Term: `AgentHealth`.
- **recovery probe** / **host health**: on-demand in-container diagnostics with tier classification. Term: `DispatchTier`.
- **workspace readiness**: creation-time blocking health check. No public type name — could be formalized as `WorkspaceReadinessProbe`.

Recommended consolidation: `_OFFLINE_HOST_STATES` should be defined once (as `frozenset[HostState]`) and imported by `recovery_probe.py` instead of duplicated as strings.

---

## Cross-Cutting Inconsistencies (Headline Findings)

- **"service" vs "application" naming collision**: A `services.toml` entry is a "service"; a service that registers a port is an "application" in `runtime/applications.toml`. But `app_watcher` emits events named `service_registered` / `service_deregistered` for application registrations, and `ServiceLogRecord` / `ServiceName` in `backend_resolver.py` use "service" for what is actually an "application". This is the most pervasive inconsistency.

- **"workspace" vs "mind" vs "agent"**: No Python class named `Mind` exists. "Workspace" is the code-canonical term (labels, function names). "Mind" appears only in `MindLiveness` and UI copy. A "workspace" is identified by the system-services `AgentId`, but users think of it as the chat agent + container.

- **Dual meaning of "template"**: `--template` in `mngr create` (a CLI preset) vs. "the forever-claude-template" (the FCT git repo). Both are legitimately called "template" in different layers of the stack.

- **Duplicate `_OFFLINE_HOST_STATES`**: Defined as `frozenset[HostState]` in `mind_liveness.py:50` and as `frozenset[str]` in `recovery_probe.py:270`. Different types for the same semantic constant.

- **Service restart policy has only two values** (`"never"` / `"on-failure"`) with no `"always"` option. An exited service with exit code 0 under `"on-failure"` is left dead permanently (not restarted). This is intentional for `deferred-install` but may surprise users who expect `"on-failure"` to also cover completed-successfully-once services.

- **`AgentHealth` uses `str, Enum` not `UpperCaseStrEnum`**: Inconsistent with other enums in the same codebase (`MindLiveness`, `LaunchMode`, etc.).

- **"system-services" agent is both the primary workspace agent and a service name**: The tmux window `svc-system_interface` (from `services.toml`) and the mngr agent `system-services` share semantic territory but are distinct concepts (one is a tmux window managed by bootstrap; the other is an mngr agent that *runs* the bootstrap).
