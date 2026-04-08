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

Each mind is created from a template repository (or local directory). The repo's own `.mngr/settings.toml` drives all configuration -- agent types, templates, environment variables, and other settings. There is no `minds.toml`, vendoring, or parent tracking.

## Configuration

All configuration lives in the template repository's `.mngr/settings.toml`. The forwarding server passes `--template main` plus a mode-specific template (`--template dev` for DEV mode, `--template docker` for LOCAL mode) when running `mngr create`. The template's settings file defines everything the agent needs.

## Data and servers

Minds use space in the host volume (via the agent dir) for persistent data. The structure and format of this data is up to each individual mind. You can optionally configure them to store their memories in git (but that is less secure, as data would leak out if synced).

Minds *must* serve web requests on one or more ports. On startup, they write JSON records to `$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl` -- one line per server -- containing the server name and URL, e.g. `{"server": "web", "url": "http://127.0.0.1:9100"}`. An agent may write multiple records for different servers (e.g. a "web" UI server and an "api" backend server). Later entries for the same server name override earlier ones. The forwarding server reads this via `mngr events <agent-id> servers/events.jsonl` to discover all backends.

# Forwarding server

The forwarding server handles routing and authentication so that the URLs being served by the mind are accessible remotely.

See [the forwarding server design doc](../imbue/minds/forwarding_server/README.md) for more details on how it is implemented.

## Agent creation

When a user visits the forwarding server and no agents exist, they are shown a creation form where they can provide a git repository URL or local path. The forwarding server:

1. Clones the repository to a temp directory (if a URL) or uses the local path directly
2. Runs `mngr create <name> --id <id> --no-connect --label mind=<name> --template main --template <mode>` to create the agent
3. Creates a Cloudflare tunnel (if configured) and injects the tunnel token into the agent via `mngr exec`
4. Redirects the user to the newly created agent (the user is already authenticated via the global session)

Agent creation is also available via the `/api/create-agent` API endpoint, which accepts a JSON body with `git_url` (a URL or local path) and returns the agent ID for status polling.

### Cloudflare tunnel integration

When the forwarding server is configured with Cloudflare credentials (via `CLOUDFLARE_FORWARDING_URL`, `CLOUDFLARE_FORWARDING_USERNAME`, `CLOUDFLARE_FORWARDING_SECRET`, and `OWNER_EMAIL` environment variables), it creates a Cloudflare tunnel for each new agent. The tunnel provides global access to the agent's services with a Google OAuth access policy gated on the owner's email.

The per-agent servers page shows both local forwarding links and global Cloudflare links, with toggle controls for enabling/disabling global forwarding per service.

# Command line interface

- `mind forward` (starts the local forwarding server for accessing and creating minds)

# Deferred items

The following are planned but not in the initial implementation:

- [future] Remote forwarding server deployment (e.g. to Modal) for access from anywhere
- [future] Mobile notifications from minds
- [future] Desktop client / system tray icon
- [future] Multi-agent interaction between minds
- [future] Offline agent handling (serving cached pages when agent is not running)
