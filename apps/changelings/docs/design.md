# Overview

See the [README](../README.md) for an overview of what changelings are and the terminology used throughout.

# Relationship to mng

Changelings are built on top of `mng` and should interact with it exclusively through the `mng` CLI interface. Changelings should never directly access mng's internal data directories (e.g., `~/.mng/agents/`). Instead, use `mng` commands like `mng list`, `mng events`, `mng exec`, etc. This ensures changelings remain compatible as mng's internals evolve and work correctly across all provider backends (local, modal, docker).

# Design principles

1. **Simplicity**: The system should be as simple as possible, both in terms of user experience and internal architecture. Each changeling is simply a web server with some persistent storage (ideally just a file system) that, by convention, ends up calling an AI agent to respond to messages from the user. The only required routes are for the index and for handling incoming messages.
2. **Personal**: Changelings are designed to serve an *individual* user. They may respond to requests from other humans (or agents), but only to the extent that they are configured to do so by their primary human user.
3. **Open**: Changelings are both transparent (the user should always be able to see exactly what is going on and dive into any detail they want) and extensible (the user should be able to easily add new capabilities, and to modify or remove existing ones).
4. **Trustworthy**: Changelings should take security and safety seriously. They should have minimal access to data that they do not need, and for the minimal amount of time that they need it.

# Architecture for changeling agents

For local deployments, each changeling has its own repo stored at `~/.changelings/<agent-id>/`. This repo is created by cloning from a git remote, or constructed from scratch via `changeling deploy --agent-type`. The agent runs directly in this directory (via `mng create --in-place`) and should make commits there if it changes anything. You can optionally link the code to a git remote in case you want the agent to push changes and make debugging easier.

For remote deployments (Modal, Docker), a temporary repo is prepared and the code is copied to the remote host via `mng create --in <provider> --source-path <temp-dir>`. The temporary repo is cleaned up after deployment.

## Agent type

The agent type is passed directly to `mng create --agent-type <type>` during deployment. The type is resolved from (in order of precedence):

1. The `--agent-type` CLI flag on `changeling deploy`
2. The `agent_type` field in `changelings.toml` in the repo

```toml
# changelings.toml
agent_type = "elena-code"
```

## Settings

Changelings read per-deployment settings from `changelings.toml` in the agent work directory (`$MNG_AGENT_WORK_DIR/changelings.toml`). This file is optional -- if it does not exist, all settings use their built-in defaults.

The settings are modeled by `ClaudeChangelingSettings` in `imbue.mng_claude_changeling.data_types`.

Bash scripts read settings via python3 one-liners with fallback defaults. Python tool scripts (deployed as standalone files to the agent host) read the TOML file directly at module load time.

## Data and servers

Changelings use space in the host volume (via the agent dir) for persistent data. The structure and format of this data is up to each individual changeling. You can optionally configure them to store their memories in git (but that is less secure, as data would leak out if synced).

Changelings *must* serve web requests on one or more ports. On startup, they write JSON records to `$MNG_AGENT_STATE_DIR/events/servers/events.jsonl` -- one line per server -- containing the server name and URL, e.g. `{"server": "web", "url": "http://127.0.0.1:9100"}`. An agent may write multiple records for different servers (e.g. a "web" UI server and an "api" backend server). Later entries for the same server name override earlier ones. The forwarding server reads this via `mng events <agent-id> servers/events.jsonl` to discover all backends.

# Forwarding server

The forwarding server handles routing and authentication so that the URLs being served by the changeling are accessible remotely.

See [the forwarding server design doc](../imbue/changelings/forwarding_server/README.md) for more details on how it is implemented.

# Command line interface

- `changeling deploy <git-url>` (clones a git repo and deploys a changeling from it)
- `changeling deploy --agent-type <type>` (creates a changeling from scratch for the given agent type)
- `changeling deploy ... --add-path SRC:DEST` (copies extra files into the changeling repo, works with both modes)
- `changeling update <agent-name>` (updates an existing changeling by snapshotting, stopping, pushing new code, re-provisioning, and restarting)
- `changeling list` (lists deployed changelings with their current state)
- `changeling forward` (starts the local forwarding server for accessing changelings)

[future] Additional commands for managing deployed changelings (stop, start, destroy, logs, etc.)

# Deferred items

The following are planned but not in the initial implementation:

- [future] Remote forwarding server deployment (e.g. to Modal) for access from anywhere
- [future] Mobile notifications from changelings
- [future] Desktop client / system tray icon
- [future] Multi-agent interaction between changelings
- [future] Offline agent handling (serving cached pages when agent is not running)
