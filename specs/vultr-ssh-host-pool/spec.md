# Vultr SSH Host Pool

## Overview

- The current `mngr create agent@host.vultr` flow takes minutes because it provisions a VPS, installs Docker, builds a container image, and starts an agent from scratch. This is the main bottleneck for minds project creation.
- This feature introduces a **host pool**: pre-provisioned Vultr VPSes with fully-configured Docker containers and stopped mngr agents, ready for instant assignment to authenticated users.
- A new **remote_service_connector** endpoint (`POST /hosts/lease`) atomically assigns an available host to a user, SSHes into the VPS and container to add the user's SSH public key, and returns connection details. A corresponding `POST /hosts/{host_db_id}/release` marks a host for cleanup.
- Pool state lives in a **Neon.tech PostgreSQL database** (not JSONL) to avoid race conditions when multiple users lease concurrently. The remote_service_connector remains mostly a thin DB + SSH layer.
- On the **client side**, a new `LaunchMode.LEASED` in `agent_creator.py` skips `mngr create` entirely. Instead, it calls the connector to get host info, writes a dynamic host entry for the SSH provider, then runs `mngr rename` and `mngr start` against the already-provisioned agent.
- The **SSH provider** gains dynamic host discovery: it reads a `dynamic_hosts.toml` file in its provider directory, allowing leased hosts to be registered without modifying static settings.
- Pool creation and cleanup remain **external scripts** (`apps/remote_service_connector/scripts/`), keeping the connector simple. The creation script uses the normal `mngr create pool-agent@host.vultr` flow, installs a management SSH key, stops the agent, and writes metadata to the DB. The cleanup script runs `mngr destroy` on released hosts.

## Expected Behavior

### Leasing a host

- User clicks "create workspace" in the minds desktop client with `LaunchMode.LEASED` selected
- The desktop client generates (or reuses) a dedicated SSH keypair at `~/.{minds_root_name}/ssh/keys/leased_host/`
- The desktop client calls `POST /hosts/lease` on the remote_service_connector with the user's SuperTokens JWT, the SSH public key, and a `version` tag (e.g. `v0.1.0` or a git hash during development)
- The connector authenticates via SuperTokens JWT (same as existing tunnel endpoints)
- The connector atomically selects an available host with a matching version from the `pool_hosts` table (`SELECT ... WHERE status = 'available' AND version = $1 ... FOR UPDATE SKIP LOCKED`)
- The connector SSHes into the VPS as root using the management key and appends the user's public key to `~/.ssh/authorized_keys` on the VPS
- The connector then SSHes into the Docker container (via its mapped SSH port on the VPS) and appends the same public key there
- The connector updates the DB row to `status = 'leased'`, records the user ID and timestamp
- The connector returns: VPS IP, SSH port (VPS-level), container SSH port, agent ID, host ID, ssh_user, and version
- The desktop client writes a `dynamic_hosts.toml` entry for the SSH provider with the container's connection details
- The desktop client runs `mngr rename <agent_id> <user-chosen-name>` and `mngr start <agent_id>` against the leased host
- The agent starts up and is usable within seconds of the initial request

### Releasing a host

- When the user is done with a workspace, the minds desktop client removes the dynamic host entry from `dynamic_hosts.toml`
- The client calls `POST /hosts/{host_db_id}/release` with the SuperTokens JWT
- The connector verifies the caller owns the lease (user ID matches `leased_to_user`)
- The connector updates the DB row to `status = 'released'` and records the timestamp
- A separate cleanup script periodically runs `mngr destroy` on released hosts and deletes the DB row

### Pool empty

- If no hosts with the requested version are available, the connector returns HTTP 503 with a clear message: "No pre-created agents are currently ready. Please ask Josh to provision more."
- The desktop client surfaces this error to the user

### Pool creation (script)

- Operator runs `scripts/create_pool_hosts.py` with a target count and version tag
- For each host: runs `mngr create pool-agent@host.vultr`, waits for the agent to be fully provisioned, then runs `mngr stop pool-agent`
- SSHes into the VPS using the Vultr-generated key and appends the management SSH public key to `~/.ssh/authorized_keys` on both the VPS and the Docker container
- Inserts a row into `pool_hosts` with status `available`, the VPS IP, Vultr instance ID, agent ID, host ID, container SSH port, and version tag

### Pool cleanup (script)

- Operator runs `scripts/cleanup_released_hosts.py`
- Reads all rows with `status = 'released'` from the DB
- For each: runs `mngr destroy <agent_id>` (which handles Docker cleanup, host record removal, and VPS destruction via the Vultr API)
- Deletes the DB row after successful destruction

### SSH provider dynamic hosts

- The SSH provider's `discover_hosts` method reads both static hosts from config and dynamic hosts from `dynamic_hosts.toml` in the provider directory
- The file uses the same `SSHHostConfig` structure as static config (address, port, user, key_file, known_hosts_file)
- The file is re-read on every discovery call (no caching)
- The minds desktop client manages this file: writes entries after successful lease, removes entries before release

## Implementation Plan

### Database

- **`pool_hosts` table** (created manually in Neon):

```sql
CREATE TABLE pool_hosts (
    id              SERIAL PRIMARY KEY,
    vps_ip          TEXT NOT NULL,
    vps_instance_id TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    host_id         TEXT NOT NULL,
    ssh_port        INTEGER DEFAULT 22,
    ssh_user        TEXT DEFAULT 'root',
    container_ssh_port INTEGER NOT NULL,
    status          TEXT DEFAULT 'available',
    version         TEXT NOT NULL,
    leased_to_user  TEXT,
    leased_at       TIMESTAMPTZ,
    released_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

- **Modal secret** `neon-<env>` with `DATABASE_URL` connection string
- **Modal secret** `pool-ssh-<env>` with `POOL_SSH_PRIVATE_KEY` (the management SSH private key, PEM-encoded)

### remote_service_connector (`apps/remote_service_connector/imbue/remote_service_connector/app.py`)

- Add `psycopg2-binary` and `paramiko` to the Modal image pip_install
- Add `neon-<env>` and `pool-ssh-<env>` to the Modal function's secrets list
- New request/response models:
  - `LeaseHostRequest(BaseModel)`: `ssh_public_key: str`, `version: str`
  - `LeaseHostResponse(BaseModel)`: `host_db_id: int`, `vps_ip: str`, `ssh_port: int`, `ssh_user: str`, `container_ssh_port: int`, `agent_id: str`, `host_id: str`, `version: str`
- New endpoints:
  - `POST /hosts/lease` -- authenticates via SuperTokens JWT (`require_admin`), atomically selects and locks an available host matching the requested version, SSHes in to add the user's public key (VPS + container), updates status to `leased`, returns host info
  - `POST /hosts/{host_db_id}/release` -- authenticates via SuperTokens JWT, verifies ownership, updates status to `released`
  - `GET /hosts` -- authenticates via SuperTokens JWT, returns the caller's leased hosts
- Inline SSH helper function `_append_authorized_key(host: str, port: int, user: str, management_key_pem: str, public_key_to_add: str) -> None` -- uses paramiko to connect with the management key and append a line to `~/.ssh/authorized_keys`
- Inline DB helper `_get_db_connection()` that reads `DATABASE_URL` from env and returns a psycopg2 connection

### SSH provider dynamic hosts (`libs/mngr/imbue/mngr/providers/ssh/`)

- **`config.py`**: Add `dynamic_hosts_file: Path | None` field to `SSHProviderConfig` with default `None`
- **`backend.py`**: In `build_provider_instance`, resolve the dynamic hosts file path. If not explicitly configured, default to `<provider_dir>/dynamic_hosts.toml` where `provider_dir` is derived from the mngr context (e.g. `~/.mngr/providers/<instance-name>/`)
- **`instance.py`**: Modify `discover_hosts` to read the dynamic hosts file (if it exists) in addition to static hosts. Parse it as TOML with `SSHHostConfig` entries per section. Merge with static hosts (static takes precedence on name collision). Also update `get_host` and `get_connector` to check dynamic hosts
- **Dynamic hosts file format** (`dynamic_hosts.toml`):
  ```toml
  [my-leased-host]
  address = "203.0.113.10"
  port = 2222
  user = "root"
  key_file = "~/.minds/ssh/keys/leased_host/id_ed25519"
  ```

### Minds desktop client

- **`apps/minds/imbue/minds/primitives.py`**: Add `LEASED = auto()` to `LaunchMode`
- **`apps/minds/imbue/minds/desktop_client/host_pool_client.py`** (new file): `HostPoolClient(FrozenModel)` with:
  - `connector_url: AnyUrl` -- base URL of remote_service_connector
  - `lease_host(access_token: str, ssh_public_key: str, version: str) -> LeaseHostResult` -- calls `POST /hosts/lease`
  - `release_host(access_token: str, host_db_id: int) -> bool` -- calls `POST /hosts/{host_db_id}/release`
  - `list_leased_hosts(access_token: str) -> list[LeasedHostInfo]` -- calls `GET /hosts`
  - Data types: `LeaseHostResult(FrozenModel)` with host_db_id, vps_ip, ssh_port, ssh_user, container_ssh_port, agent_id, host_id, version
- **`apps/minds/imbue/minds/desktop_client/agent_creator.py`**:
  - Update `_build_mngr_create_command` match statement: `LaunchMode.LEASED` raises an error (this mode doesn't use `mngr create`)
  - Update `_create_agent_background`: for `LaunchMode.LEASED`, call the `HostPoolClient` to lease a host, generate/load the SSH keypair from `~/.{minds_root_name}/ssh/keys/leased_host/`, write the dynamic hosts TOML entry, run `mngr rename` and `mngr start`
  - New helper `_load_or_create_leased_host_keypair(data_dir: Path) -> tuple[Path, str]` that returns `(private_key_path, public_key_string)` -- generates an ed25519 keypair if not present
  - New helper `_write_dynamic_host_entry(provider_dir: Path, host_name: str, address: str, port: int, user: str, key_file: Path) -> None`
  - New helper `_remove_dynamic_host_entry(provider_dir: Path, host_name: str) -> None`
- **`apps/minds/imbue/minds/desktop_client/templates.py`**: Add `LEASED` to the launch mode options in the UI (if rendered in the template)
- **`apps/minds/imbue/minds/desktop_client/runner.py`**: Wire up `HostPoolClient` in the runner's dependency setup, using `minds_config.remote_service_connector_url`

### Pool management scripts (`apps/remote_service_connector/scripts/`)

- **`create_pool_hosts.py`**: CLI script (click-based) that:
  - Takes `--count N` (number of hosts to create), `--version TAG` (version label), and optional `--region`, `--plan` args
  - For each host: runs `mngr create pool-agent@<host-name>.vultr --no-connect`, waits for completion, runs `mngr stop pool-agent`
  - Reads the Vultr-generated SSH key from local mngr state
  - SSHes to VPS as root, appends management public key to VPS `~/.ssh/authorized_keys`
  - SSHes to VPS as root, runs `docker exec` to append management public key inside the container's `~/.ssh/authorized_keys`
  - Extracts agent ID, host ID, VPS IP, container SSH port from mngr state
  - Inserts a row into `pool_hosts` via direct Neon DB connection
- **`cleanup_released_hosts.py`**: CLI script that:
  - Reads all `status = 'released'` rows from `pool_hosts`
  - For each: runs `mngr destroy <agent_id>`
  - Deletes the DB row on success
- **`generate_management_key.py`**: One-time script to generate the ed25519 management keypair and print instructions for uploading to Modal secrets

## Implementation Phases

### Phase 1: Database and management key setup

- Create the `pool_hosts` table in Neon manually
- Generate the management SSH keypair via `generate_management_key.py`
- Upload the management private key to Modal as `pool-ssh-<env>` secret
- Upload the Neon `DATABASE_URL` to Modal as `neon-<env>` secret
- **Result**: Infrastructure is ready for pool creation and the connector to use

### Phase 2: SSH provider dynamic hosts

- Add `dynamic_hosts_file` to `SSHProviderConfig`
- Implement dynamic host file reading in `SSHProviderInstance.discover_hosts`, `get_host`, and `get_connector`
- Update `SSHProviderBackend.build_provider_instance` to resolve the dynamic hosts path
- Write unit tests for dynamic host discovery (file present, file absent, file malformed, merge with static hosts)
- **Result**: mngr can discover and connect to dynamically-registered SSH hosts

### Phase 3: Pool creation script

- Implement `create_pool_hosts.py`
- Implement `generate_management_key.py`
- Test: create a small pool (1-2 hosts), verify DB rows are correct, verify management key is installed on VPS and container
- **Result**: Operator can populate the host pool

### Phase 4: Remote connector lease/release endpoints

- Add psycopg2-binary and paramiko to the Modal image
- Add new secrets to the Modal function
- Implement `POST /hosts/lease` with DB locking and SSH key injection
- Implement `POST /hosts/{host_db_id}/release` with ownership verification
- Implement `GET /hosts` for listing
- Write unit tests (mock DB and SSH)
- Deploy to Modal
- **Result**: Authenticated users can lease and release hosts via the API

### Phase 5: Desktop client integration

- Add `LEASED` to `LaunchMode`
- Implement `HostPoolClient` in `host_pool_client.py`
- Implement `_load_or_create_leased_host_keypair` in `agent_creator.py`
- Implement dynamic host entry write/remove helpers
- Implement the `LaunchMode.LEASED` path in `_create_agent_background`
- Wire up `HostPoolClient` in `runner.py`
- Add `LEASED` option to the UI template
- Write unit tests for the new client and agent creator flow
- **Result**: End-to-end flow works -- user can lease a host, rename the agent, start it, use it, and release it

### Phase 6: Cleanup script

- Implement `cleanup_released_hosts.py`
- Test: release a host via the API, run cleanup, verify VPS is destroyed and DB row is deleted
- **Result**: Full lifecycle is operational

## Testing Strategy

### Unit tests

- **SSH provider dynamic hosts** (`libs/mngr/imbue/mngr/providers/ssh/instance_test.py`):
  - `test_discover_hosts_includes_dynamic_hosts_from_file` -- dynamic hosts appear in discovery
  - `test_discover_hosts_static_takes_precedence_over_dynamic` -- name collision uses static
  - `test_discover_hosts_ignores_missing_dynamic_file` -- no crash if file doesn't exist
  - `test_discover_hosts_ignores_malformed_dynamic_file` -- graceful handling of bad TOML
  - `test_get_host_finds_dynamic_host` -- get_host resolves a host from the dynamic file

- **HostPoolClient** (`apps/minds/imbue/minds/desktop_client/host_pool_client_test.py`):
  - Test lease/release/list methods against a mock HTTP server (using `httpx`-compatible test patterns)
  - Test error handling (503 pool empty, 403 ownership mismatch, network errors)

- **Agent creator leased flow** (`apps/minds/imbue/minds/desktop_client/agent_creator_test.py`):
  - Test `_load_or_create_leased_host_keypair` generates and reuses keys
  - Test `_write_dynamic_host_entry` and `_remove_dynamic_host_entry` produce valid TOML
  - Test `LaunchMode.LEASED` code path in `_create_agent_background` (mock the HostPoolClient and mngr commands)

- **Remote connector** (`apps/remote_service_connector/imbue/remote_service_connector/app_test.py`):
  - Test lease endpoint: available host is returned, DB is updated, SSH key injection is called
  - Test lease endpoint: pool empty returns 503
  - Test lease endpoint: version mismatch returns 503
  - Test release endpoint: ownership verified, status updated
  - Test release endpoint: wrong user gets 403

### Integration tests

- **SSH provider**: Create a temp directory with a `dynamic_hosts.toml`, build an `SSHProviderInstance`, verify full discover/get_host flow

### Acceptance tests

- **End-to-end lease flow** (requires real Vultr VPS in pool):
  - Lease a host, verify SSH access works, rename agent, start agent, verify agent is running, release host

## Open Questions

- **SSH provider directory path**: The spec assumes `~/.mngr/providers/<instance-name>/` for the dynamic hosts file, but the SSH provider may not have a dedicated directory today. Need to verify what `mngr_ctx` provides and whether a provider-specific directory is conventionally created.
- **mngr rename and start over SSH**: The client runs `mngr rename <agent_id> <new_name>` and `mngr start <agent_id>` -- need to verify these commands work correctly when the agent was created by a different mngr instance (different machine). The SSH provider discovers agents by SSHing in and reading state files, so this should work, but hasn't been tested with cross-machine agent management.
- **Container SSH port discovery**: The pool creation script needs to extract the container's mapped SSH port from mngr state. Need to verify where this is stored (likely in `VpsDockerHostRecord.certified_host_data` or the container's port mapping) and how to read it programmatically.
- **Concurrent lease atomicity**: The `SELECT ... FOR UPDATE SKIP LOCKED` pattern should handle concurrent requests, but need to verify psycopg2's transaction isolation level defaults and whether Modal's serverless cold-start behavior affects DB connection pooling.
- **UI integration**: The spec adds `LEASED` to `LaunchMode` but doesn't detail the UI flow for selecting it. Need to decide whether this is an explicit user choice or automatically selected when the user is authenticated and a pool is available.
