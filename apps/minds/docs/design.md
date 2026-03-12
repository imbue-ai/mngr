# Overview

See the [README](../README.md) for an overview of what minds are and the terminology used throughout.

# Relationship to mng

Minds are built on top of `mng` and should interact with it exclusively through the `mng` CLI interface. Minds should never directly access mng's internal data directories (e.g., `~/.mng/agents/`). Instead, use `mng` commands like `mng list`, `mng events`, `mng exec`, etc. This ensures minds remain compatible as mng's internals evolve and work correctly across all provider backends (local, modal, docker).

# Design principles

1. **Simplicity**: The system should be as simple as possible, both in terms of user experience and internal architecture. Each mind is simply a web server with some persistent storage (ideally just a file system) that, by convention, ends up calling an AI agent to respond to messages from the user. The only required routes are for the index and for handling incoming messages.
2. **Personal**: Minds are designed to serve an *individual* user. They may respond to requests from other humans (or agents), but only to the extent that they are configured to do so by their primary human user.
3. **Open**: Minds are both transparent (the user should always be able to see exactly what is going on and dive into any detail they want) and extensible (the user should be able to easily add new capabilities, and to modify or remove existing ones).
4. **Trustworthy**: Minds should take security and safety seriously. They should have minimal access to data that they do not need, and for the minimal amount of time that they need it.

# Architecture for mind agents

For local deployments, each mind has its own repo stored at `~/.minds/<agent-id>/`. This repo is created by cloning from a git remote, or constructed from scratch via `mind deploy --agent-type`. The agent runs directly in this directory (via `mng create --in-place`) and should make commits there if it changes anything. You can optionally link the code to a git remote in case you want the agent to push changes and make debugging easier.

For remote deployments (Modal, Docker), a temporary repo is prepared and the code is copied to the remote host via `mng create --in <provider> --source-path <temp-dir>`. The temporary repo is cleaned up after deployment.

## Agent type

The agent type is passed directly to `mng create --agent-type <type>` during deployment. The type is resolved from (in order of precedence):

1. The `--agent-type` CLI flag on `mind deploy`
2. The `agent_type` field in `minds.toml` in the repo

```toml
# minds.toml
agent_type = "elena-code"
```

## Settings

Minds read per-deployment settings from `minds.toml` in the agent work directory (`$MNG_AGENT_WORK_DIR/minds.toml`). This file is optional -- if it does not exist, all settings use their built-in defaults.

The settings are modeled by `ClaudeMindSettings` in `imbue.mng_claude_mind.data_types`.

Bash scripts read settings via python3 one-liners with fallback defaults. Python tool scripts (deployed as standalone files to the agent host) read the TOML file directly at module load time.

## Data and servers

Minds use space in the host volume (via the agent dir) for persistent data. The structure and format of this data is up to each individual mind. You can optionally configure them to store their memories in git (but that is less secure, as data would leak out if synced).

Minds *must* serve web requests on one or more ports. On startup, they write JSON records to `$MNG_AGENT_STATE_DIR/events/servers/events.jsonl` -- one line per server -- containing the server name and URL, e.g. `{"server": "web", "url": "http://127.0.0.1:9100"}`. An agent may write multiple records for different servers (e.g. a "web" UI server and an "api" backend server). Later entries for the same server name override earlier ones. The forwarding server reads this via `mng events <agent-id> servers/events.jsonl` to discover all backends.

# Forwarding server

The forwarding server handles routing and authentication so that the URLs being served by the mind are accessible remotely.

See [the forwarding server design doc](../imbue/minds/forwarding_server/README.md) for more details on how it is implemented.

# Command line interface

- `mind deploy <git-url>` (clones a git repo and deploys a mind from it)
- `mind deploy --agent-type <type>` (creates a mind from scratch for the given agent type)
- `mind deploy ... --add-path SRC:DEST` (copies extra files into the mind repo, works with both modes)
- `mind update <agent-name>` (updates an existing mind by snapshotting, stopping, pushing new code, re-provisioning, and restarting)
- `mind list` (lists deployed minds with their current state)
- `mind forward` (starts the local forwarding server for accessing minds)

[future] Additional commands for managing deployed minds (stop, start, destroy, logs, etc.)

# Deferred items

The following are planned but not in the initial implementation:

- [future] Remote forwarding server deployment (e.g. to Modal) for access from anywhere
- [future] Mobile notifications from minds
- [future] Desktop client / system tray icon
- [future] Multi-agent interaction between minds
- [future] Offline agent handling (serving cached pages when agent is not running)
