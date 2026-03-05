## ARCHITECTURE OVERVIEW REPORT

Based on thorough exploration of the mng monorepo codebase, here is a detailed analysis of the runtime execution model and infrastructure:

### EXECUTION MODEL

**Core Architecture:**
- **Agent-Centric State Model**: `mng` implements a stateless, convention-based design where agents contain their own complete state on their host. There is no central persistent state store or database.
- **No Persistent Processes**: `mng` itself is entirely stateless - it reconstructs all state by:
  - Querying providers (Docker labels, Modal tags, local state files)
  - Querying hosts via SSH to check process liveness and read agent filesystem state
  - Reading configuration files
  - Multiple `mng` instances can manage the same agents simultaneously

**Execution Flow:**
1. **CLI Entry Point** (`/home/user/mng/libs/mng/imbue/mng/main.py`):
   - Uses Click for CLI framework
   - Plugin-based architecture via pluggy hooks system
   - Commands registered as Click command group with aliases
   - Default command: `create` (launching agents)

2. **Agent Lifecycle**:
   - Agents are processes running in tmux sessions with `mng-` prefix
   - Each agent has a unique ID and state directory at `$MNG_HOST_DIR/agents/<agent_id>/`
   - Agent state stored in `data.json` (command, permissions, labels, created_branch_name, start_on_boot)
   - Agents run on **hosts** - either local machine or remote (Modal, Docker, etc.)

3. **Process Execution**:
   - Uses `ConcurrencyGroup` library (`/home/user/mng/libs/concurrency_group/`) for managing threads and processes
   - `ConcurrencyGroup` tracks lifecycle of concurrent operations, handles cleanup, propagates shutdown events
   - Context manager pattern ensures proper cleanup on exit
   - Supports background execution with `run_background()`

4. **Host Types**:
   - **Local**: Direct tmux sessions on local machine via pyinfra local connector
   - **Modal**: Sandboxes via Modal cloud platform with persistent apps and volumes
   - **Docker**: Containers with optional custom images

### STATE MANAGEMENT AND STORAGE

**Agent State Storage:**
- **Local**: `$HOME/.mng/` directory contains configuration
- **Per-Host**: State stored at `$MNG_HOST_DIR/agents/<agent_id>/`:
  - `data.json`: Certified agent metadata (command, permissions, labels, branches, boot settings)
  - Subdirectories for: work_dir (agent's working directory), logs, activity files
  - Activity files track idle detection (`$MNG_HOST_DIR/activity/user`, `$MNG_HOST_DIR/activity/agent`, etc.)

**Host State Storage:**
- Hosts store state in `$MNG_HOST_DIR/` with subdirectories: agents/, volumes/, activity/
- State files include provider-specific metadata (Docker container IDs, Modal sandbox IDs, etc.)
- Cooperative file locking via `fcntl.flock()` prevents race conditions
- External storage: Modal volumes persist agent data between sandbox sessions

**Configuration:**
- User config: `~/.mng/config.toml`
- Profile-based settings stored in `~/.mng/profiles/`
- Provider instances configured with backend-specific settings (host_dir, build_args, start_args)
- Environment variables: `MNG_HOST_DIR`, `MNG_PREFIX`, `PYTEST_NUMPROCESSES`, `PYTEST_MAX_DURATION`, Modal credentials, etc.

**Data Persistence Strategy:**
- Minimal state by design - leverage host filesystem and provider APIs
- File-based activity tracking for idle detection (mtime-based timestamps)
- JSON for structured data (config, agent metadata, certified host data)
- External persistence for Modal: volumes managed via Modal SDK

### EXTERNAL DEPENDENCIES AND APIS

**Cloud Providers:**
1. **Modal** (`/home/user/mng/libs/mng/imbue/mng/providers/modal/`):
   - Uses Modal Python SDK for sandbox provisioning
   - Creates persistent apps and sandboxes
   - State volumes for persistent storage between runs
   - Modal environments manage resource scoping
   - Automatic retry logic for NotFoundError during environment creation

2. **Docker** (`/home/user/mng/libs/mng/imbue/mng/providers/docker/`):
   - Creates and manages Docker containers
   - Custom images via build args
   - Labels for metadata storage
   - Docker daemon-based operations

**System Tools:**
- **SSH**: Universal host access mechanism (local or remote)
- **tmux**: Session/window/pane management for agents
- **git**: Repository operations (clone, push, pull, branch management)
- **rsync/unison**: File synchronization (push/pull operations)
- **pyinfra**: Connector abstraction for SSH/local execution
- **jq**: JSON processing for CLI output

**Agent Types (Extensible via Plugins):**
- **Claude**: Claude Code agent with settings sync (`~/.claude/` files)
- **Codex**: Alternative agent type
- **Skill Agent**: Plugin-based skill execution
- **Code Guardian, FixMe Fairy**: Specialized agents
- Custom command-based agents supported

**External APIs:**
- Modal API: Sandbox creation, app management, environment management, volume operations
- Claude Code API: (when agents communicate externally)

### CONFIGURATION SYSTEM

**Config Loading Pipeline:**
1. `~/.mng/config.toml`: User configuration
2. Command-line arguments override config
3. `on_load_config` plugin hooks allow dynamic modification before validation
4. Pydantic validation with custom validators

**Configuration Structure** (`/home/user/mng/libs/mng/imbue/mng/config/data_types.py`):
- **MngConfig**: Global settings (default_host_dir, prefix, destroyed_host_persisted_seconds=7 days)
- **ProviderInstanceConfig**: Provider-specific settings (backend name, host_dir, build/start args)
- **AgentTypeConfig**: Agent type definitions (command, CLI args, permissions)
- **ActivityConfig**: Idle detection settings (idle_mode, idle_timeout_seconds)
- **EnvVar**: Environment variables (key=value pairs)
- **HookDefinition**: Lifecycle hooks (NAME:COMMAND format)

**Plugin System:**
- Entry point-based plugin discovery via setuptools
- Plugins register via `@hookimpl` decorators
- Hook specs defined in `hookspecs.py`
- Disabled plugins blocked before setuptools entrypoint loading

**Key Hooks:**
- **Registration hooks**: `register_provider_backend()`, `register_agent_type()`, `register_cli_commands()`, `register_cli_options()`
- **Lifecycle hooks**: `on_before/after_create`, `on_before/after_provisioning`, `on_agent_created`, `on_before/after_destroy`
- **Configuration hooks**: `on_load_config()`, `override_command_options()`
- **Error hooks**: `on_error()`, `on_after_command()`
- **Startup/shutdown**: `on_startup()`, `on_shutdown()`

### TESTING INFRASTRUCTURE

**Test Organization:**
- **Unit tests**: `*_test.py` files - fast, focused, no network
- **Integration tests**: Default pytest runs - test major functionality
- **Acceptance tests**: `-m acceptance` marker - require network, Modal credentials, Docker
- **Release tests**: `-m release` marker - full validation (superset of acceptance)

**Shared Testing Infrastructure** (`/home/user/mng/libs/imbue_common/imbue/imbue_common/conftest_hooks.py`):
- **Global test locking**: `/tmp/pytest_global_test_lock` with fcntl prevents parallel test conflicts
- **Xdist parallelism**: Default 4 workers, configurable via `PYTEST_NUMPROCESSES`
- **Test timeouts**: 10 seconds function-level timeout (signal-based), configurable via `PYTEST_MAX_DURATION`
- **Output redirection**: `.test_output/` directory for slow tests report, coverage reports

**Key Fixtures** (from `/home/user/mng/libs/mng/imbue/mng/conftest.py`):
- `cg`: ConcurrencyGroup for process/thread management
- `mng_test_id`: Unique test identifier for cleanup tracking
- `temp_host_dir`: Temporary directory for host state
- `temp_profile_dir`: Temporary profile directory
- `plugin_manager`: Pluggy PluginManager for plugin testing
- `local_provider`: LocalProviderInstance for testing
- `temp_mng_ctx`: MngContext for testing
- Modal-specific fixtures: `modal_mng_ctx`, `make_modal_provider_real()`

**Coverage Requirements:**
- Minimum 80% coverage (hard requirement)
- 81% warning threshold for early detection
- Excluded files: `_test.py`, `test_*.py`, TUI files, Modal/Docker providers (require infrastructure)
- Concurrency mode: multiprocessing (parallel coverage tracking)

**Test Execution:**
- Unit tests: `uv run pytest --ignore-glob="**/test_*.py"` (no acceptance/release)
- Integration: `uv run pytest` (default with xdist)
- Acceptance: `-m "not release"` with 90-second timeout
- Release: full suite with 90-second timeout
- On Modal: Via Offload tool with test environment cleanup

### BUILD AND CI/CD

**Local Development:**
- **justfile** targets: `test-unit`, `test-integration`, `test-quick`, `test-acceptance`, `test-release`, `test-timings`, `test <path>`
- **uv package manager**: Workspace-based monorepo with members in `libs/*` and `apps/*`
- **Pre-commit hooks**: Ruff linting via `.pre-commit-config.yaml`

**GitHub Actions CI** (`.github/workflows/ci.yml`):
1. **test-integration** (non-release branches):
   - 4 parallel groups with pytest-split using least_duration algorithm
   - Runs unit + integration tests
   - Coverage combines across workers (download artifacts, merge)
   - Coverage warning job at 81% threshold

2. **test-acceptance** (all except release branch):
   - 16 parallel groups for longer-running acceptance tests
   - 90-second timeout per test
   - Requires Modal credentials (`MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`)
   - Docker Hub mirror configured for faster builds

3. **test-release** (release branch pushes only):
   - 12 parallel groups
   - Full suite including all acceptance tests
   - Same 90-second timeout

4. **cleanup-modal-environments**:
   - Runs on all branches
   - Deletes Modal test environments older than 1 hour
   - Uses cleanup script (`scripts/cleanup_old_modal_test_environments.py`)

5. **Additional Checks**:
   - Type checking: `test_no_type_errors` (via separate workflow)
   - Import linting: `importlinter` for layer enforcement
   - Ruff formatting/linting

**Dependency Management:**
- `pyproject.toml` workspace configuration with all projects
- `uv.lock` for deterministic builds
- Dev dependencies: pytest, coverage, pytest-cov, pytest-xdist, ruff, pre-commit
- Package builds via `pyproject-build` for distribution

**Key Infrastructure Files:**
- `/home/user/mng/justfile`: Development task automation
- `/home/user/mng/.github/workflows/`: CI/CD pipeline definitions
- `/home/user/mng/pyproject.toml`: Workspace config, pytest settings, coverage config
- `/home/user/mng/conftest.py`: Root test configuration
- `/home/user/mng/.pre-commit-config.yaml`: Code quality hooks
- `/home/user/mng/offload-modal.toml`, `/home/user/mng/offload-modal-acceptance.toml`: Offload tool config for remote test execution

**Code Quality:**
- **Ruff**: Line length 119, selective linting (E, F, B)
- **Import linting**: Enforced layer separation via importlinter
- **Type checking**: py with mypy-like validation
- **Coverage**: 80% minimum, 81% warning threshold, parallel multiprocessing mode
- **Formatters**: Ruff format (double quotes, space indent)

---

This architecture represents a sophisticated, stateless design that prioritizes simplicity, scalability, and extensibility through convention-based configuration and pluggable backend providers. The system manages distributed agent execution with minimal central coordination, relying on filesystem conventions and SSH as universal integration points.