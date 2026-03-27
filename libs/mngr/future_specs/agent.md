# Agent Spec

All agent data is stored in the folder defined by the `MNGR_AGENT_STATE_DIR` environment variable (see [conventions](../docs/conventions.md)).

The agent ID is set at creation time and is immutable.

The agent name can be determined by stripping the $MNGR_PREFIX env var from the name of the controlling tmux session.

The state of the agent is based on whether the expected `command` is running inside the first pane for the tmux session.

By convention, the agent directory also contains a subdirectory for `events/`.

The agent directory should also include an `env` file with any environment variable overrides for this agent (it will be sourced after the host-level `env` file).

## State

Agent state is separated into two classes:

- "certified": the data that `mngr` itself manages. This data is stored in `data.json` inside the agent directory, and is signed by `mngr` to prevent tampering.
- "reported": data that the agent itself manages, i.e. anything else in the agent directory. This includes status files, activity timestamps, logs, etc.

### Certified Fields

| JSON Path (within `data.json`) | Notes                                             |
|--------------------------------|---------------------------------------------------|
| `id`                           | Unique identifier.                                |
| `name`                         | Human-readable name                               |
| `type`                         | Agent type (claude, codex, etc.)                  |
| `command`                      | The command used to start the agent               |
| `work_dir`                     | Working directory for this agent                  |
| `permissions`                  | List of permissions assigned to this agent        |
| `create_time`                  | When the agent was created                        |
| `start_on_boot`                | If present and true, the agent will start at boot |
| `plugin.*`                     | Plugin-specific (certified) state                 |
| `host.*`                       | Host-specific state, see [host.md](./host.md)     |

### Reported Fields

| Field             | Notes                                        | Storage Location (within `MNGR_AGENT_STATE_DIR`) |
|-------------------|----------------------------------------------|--------------------------------------------------|
| `url`             | For communicating with the agent via http(s) | `url`                                            |
| `start_time`      | When the tmux session was started            | `activity/start` (mtime)                         |
| `runtime_seconds` | `current_time` - `start_time`                | (computed)                                       |
| `plugin.*`        | Plugin-specific (reported) state             | `plugin/<plugin>/*`                              |

**Important:** All access to agent data should be through methods that communicate whether that data is "certified" or "reported", to help avoid confusion about which fields are trustworthy (ex: `get_create_time` vs `get_reported_url`).

