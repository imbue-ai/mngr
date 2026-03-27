# Conventions

The `mng` tool prefixes the names of many resources with `mng-` (this can be customized via `MNG_PREFIX` environment variable--everything below that says "mng-" will be replaced by that environment variable).

Unless otherwise specified, `mng` assumes:
- the user is either the current user (local) or `root` (remote, override via config or CLI args for most commands)
- a host name is a unique identifier for the host (a host can contain multiple agents).
- tmux sessions are named `mng-<agent_name>`
- agent data exists at `$MNG_AGENT_STATE_DIR` (i.e., `$MNG_HOST_DIR/agents/$MNG_AGENT_ID/`)
- there are `events` subdirectories inside `$MNG_HOST_DIR` and each `$MNG_AGENT_STATE_DIR` for storing structured event data (JSONL files under `events/`). Plain-text service logs (sshd, activity watcher, volume sync, shutdown) are stored under `$MNG_HOST_DIR/logs/`.
- environment variables for hosts and agents are stored in `$MNG_HOST_DIR/env` and `$MNG_AGENT_STATE_DIR/env` respectively
- IDs are base16-encoded UUID4s
- Names are human-readable strings that can contain letters, numbers, and hyphens (no underscores, spaces, etc because they are used for DNS)

`mng` automatically sets these additional environment variables inside agent tmux sessions:

- `MNG_HOST_DIR` — The base directory for all mng data within the host where the agent is running. See [host spec](../future_specs/host.md) for data layout (default: `~/.mng`).
- `MNG_AGENT_ID` — The agent's unique identifier
- `MNG_AGENT_NAME` — The agent's human-readable name
- `MNG_AGENT_STATE_DIR` — The per-agent directory for status, activity, plugins. See [agent spec](../future_specs/agent.md) for data layout (default: `$MNG_HOST_DIR/agents/$MNG_AGENT_ID/`)
- `MNG_AGENT_WORK_DIR` — The directory in which the agent is started, which contains your project files

See [environment variables](./concepts/environment_variables.md) for the full list and how to set custom variables.
