# Host Spec

## State

Most host data is queried from the provider (Docker, Modal, local, etc.) at runtime.

Some data (e.g. from plugins, logs, etc.) is stored in the host directory at `$MNG_HOST_DIR/`.  By convention, it should include a subdirectory for `events/`.

The host directory should also include a `env` file with the environment variables for agents running on the host.

Except for the contents of `data.json` (which is signed), data in the host directory is untrustworthy (since it can be modified by any process on the host).

### Certified fields

| Field                        | Notes                                                                                                                  | Storage Location |
|------------------------------|------------------------------------------------------------------------------------------------------------------------|------------------|
| `id`                         | Unique identifier, set at creation. Format is provider-dependent                                                       | Provider         |
| `provider`                   | Provider type (local, docker, modal, etc.)                                                                             | Provider         |
| `name`                       | Human-readable name                                                                                                    | Provider         |
| `tags`                       | Metadata tags for the host                                                                                             | Provider         |
| `state`                      | Derived. One of `building`, `starting`, `running`, `stopping`, `stopped`, `failed`, `destroyed`                        | (computed)       |
| `image`                      | Base image used. Format is provider-dependent                                                                          | Provider         |
| `snapshots`                  | Queried from provider                                                                                                  | Provider         |
| `ssh.*`                      | Derived from provider + local SSH config                                                                               | (computed)       |
| `resource.*`                 | Resources being used by the host. May have different keys for each provider.                                           | Provider         |
| `resource.cpu.count`         | How many CPUs assigned to the host                                                                                     | Provider         |
| `resource.cpu.frequency_ghz` | CPU frequency (in GHz) assigned to the host                                                                            | Provider         |
| `resource.memory_gb`         | How much memory (in GB) assigned to the host                                                                           | Provider         |
| `resources.disk_gb`          | How much disk space (in GB) assigned to the host                                                                       | Provider         |
| `boot_time`                  | When the host was last started/resumed                                                                                 | Provider         |
| `uptime_seconds`             | `current_time` - `boot_time`                                                                                           | (computed)       |
| `idle_seconds`               | How long since the host was active                                                                                     | (computed)       |
| `idle_mode`                  | One of `io`, `user`, `agent`, `ssh`, `create`, `boot`, `start`, `run`, `disabled`, `custom`                            | `data.json`      |
| `idle_timeout_seconds`     | Maximum idle time before stopping                                                                                      | `data.json`      |
| `activity_sources`           | What to consider as activity for idle detection (list of `create`, `boot`, `start`, `ssh`, `process`, `agent`, `user`) | `data.json`      |
| `plugin.*`                   | Plugin-specific (certified) host state                                                                                 | `data.json`      |
| `permissions`                | Union of all agent permissions on this host                                                                            | (computed)       |

### Reported fields

Note that this includes many of the underlying fields required for idle detection--there's no way to guarantee these, since, for example, an agent could always just slowly emit updates and keep updating the agent activity time.

| Field                 | Notes                                                                                                                                                         | Storage Location (in `$MNG_HOST_DIR/`) |
|-----------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------|
| `agent_activity_time` | When there was last activity from an agent                                                                                                                    | `activity/agent`                        |
| `user_activity_time`  | When there was last activity from the user                                                                                                                    | `activity/user`                         |
| `ssh_activity_time`   | When we last noticed an active ssh connection                                                                                                                 | `activity/ssh`                          |
| `is_locked`           | Cooperative locking to prevent multiple instances of mng from operating simultaneously [future: remote implementation, only works for local hosts right now] | `host_lock`                             |
| `lock_time`           | mtime of the `host_lock` file                                                                                                                                 | `host_lock`                             |
| `plugin.*`            | Plugin-specific (reported) host state                                                                                                                         | `plugin/<plugin>/*`                     |

**Important:** All access to host data should be through methods that communicate whether that data is "certified" or "reported", to help avoid confusion about which fields are trustworthy (ex: `get_provider` vs `get_reported_idle_mode`).
