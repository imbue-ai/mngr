# Overview

See the [README](../README.md) for an overview of what minds are and see [the glossary](./mind/glossary.md) for terminology used throughout.

# Relationship to mngr

Minds are built on top of `mngr` and should interact with it exclusively through the `mngr` CLI interface. Minds should never directly access mngr's internal data directories (e.g., `~/.mngr/agents/`). Instead, use `mngr` commands like `mngr list`, `mngr events`, `mngr exec`, etc. This ensures minds remain compatible as mngr's internals evolve and work correctly across all provider backends (local, modal, docker).

# Design principles

1. **Simplicity**: The system should be as simple as possible, both in terms of user experience and internal architecture. Each mind is simply a web server with some persistent storage (ideally just a file system) that, by convention, ends up calling an AI agent to respond to messages from the user. The only required routes are for the index and for handling incoming messages.
2. **Personal**: Minds are designed to serve an *individual* user. They may respond to requests from other humans (or agents), but only to the extent that they are configured to do so by their primary human user.
3. **Open**: Minds are both transparent (the user should always be able to see exactly what is going on and dive into any detail they want) and extensible (the user should be able to easily add new capabilities, and to modify or remove existing ones).
4. **Trustworthy**: Minds should take security and safety seriously. They should have minimal access to data that they do not need, and for the minimal amount of time that they need it.

# Architecture for mind agents

Each mind has its own repo stored at `~/.minds/<agent-id>/`. This repo is created by cloning from a git remote when the user creates a mind via the forwarding server. The agent runs directly in this directory (via `mngr create --transfer=none`) and should make commits there if it changes anything.

## Agent type

The agent type is passed directly to `mngr create --type <type>` during creation. The type is resolved from (in order of precedence):

1. The `agent_type` field in `minds.toml` in the cloned repo
2. The default type: `claude-mind`

```toml
# minds.toml
agent_type = "elena-code"
```

## Vendor repos

During agent creation, external repositories can be added as git subtrees under `vendor/` in the mind's directory. This is configured via `[[vendor]]` entries in `minds.toml`. Each entry must specify either a remote `url` or a local `path`, and can optionally pin a specific git `ref` (defaults to the current HEAD).

Local repos must be "clean" (no uncommitted changes or untracked files) before they can be vendored.

When no `[[vendor]]` section exists, the system falls back to vendoring the `mngr` repo from its public GitHub URL.

For development, the `MINDS_VENDOR_PATH` environment variable can override vendor sources with local paths. Format: `name@/path/to/repo:other@/another/path`. Each entry overrides (or adds) a vendor config to use a local path instead of whatever was configured.

```toml
# minds.toml

# Vendor the mngr repo from GitHub at a specific commit
[[vendor]]
name = "mngr"
url = "https://github.com/imbue-ai/mngr.git"
ref = "abc123"

# Vendor a local repo (must be clean)
[[vendor]]
name = "my-lib"
path = "/path/to/local/repo"
```

## Settings

Minds read per-mind settings from `minds.toml` in the agent work directory (`$MNGR_AGENT_WORK_DIR/minds.toml`). This file is optional -- if it does not exist, all settings use their built-in defaults.

The settings are modeled by `ClaudeMindSettings` in `imbue.mngr_claude_mind.data_types`.

Bash scripts read settings via python3 one-liners with fallback defaults. Python tool scripts (deployed as standalone files to the agent host) read the TOML file directly at module load time.

## Data and servers

Minds use space in the host volume (via the agent dir) for persistent data. The structure and format of this data is up to each individual mind. You can optionally configure them to store their memories in git (but that is less secure, as data would leak out if synced).

Minds *must* serve web requests on one or more ports. On startup, they write JSON records to `$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl` -- one line per server -- containing the server name and URL, e.g. `{"server": "web", "url": "http://127.0.0.1:9100"}`. An agent may write multiple records for different servers (e.g. a "web" UI server and an "api" backend server). Later entries for the same server name override earlier ones. The forwarding server reads this via `mngr events <agent-id> servers/events.jsonl` to discover all backends.

# Forwarding server

The forwarding server handles routing and authentication so that the URLs being served by the mind are accessible remotely.

See [the forwarding server design doc](../imbue/minds/forwarding_server/README.md) for more details on how it is implemented.

## Agent creation

When a user visits the forwarding server and no agents exist, they are shown a creation form where they can provide a git repository URL. The forwarding server:

1. Clones the repository to `~/.minds/<agent-id>/`
2. Loads settings from `minds.toml` (agent type, vendor repos, etc.)
3. Adds configured vendor repos as git subtrees (or defaults to vendoring mngr)
4. Runs `mngr create --type <type> --id <id> --transfer=none --label mind=true` to start the agent
4. Redirects the user to the newly created agent (the user is already authenticated via the global session)

Agent creation is also available via the `/api/create-agent` API endpoint, which accepts a JSON body with `git_url` and returns the agent ID for status polling.

# Command line interface

- `mind forward` (starts the local forwarding server for accessing and creating minds)

[future] Additional commands for managing minds (stop, start, destroy, logs, etc.)

# Deferred items

The following are planned but not in the initial implementation:

- [future] Remote forwarding server deployment (e.g. to Modal) for access from anywhere
- [future] Mobile notifications from minds
- [future] Desktop client / system tray icon
- [future] Multi-agent interaction between minds
- [future] Offline agent handling (serving cached pages when agent is not running)
