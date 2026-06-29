# Overview

See the [README](../README.md) for an overview of what workspaces are and see [the glossary](./workspace/glossary.md) for terminology used throughout.

# Relationship to mngr

Workspaces are built on top of `mngr` and should interact with it exclusively through the `mngr` CLI interface. Workspaces should never directly access mngr's internal data directories (e.g., `~/.mngr/agents/`). Instead, use `mngr` commands like `mngr list`, `mngr event`, `mngr exec`, etc. This ensures workspaces remain compatible as mngr's internals evolve and work correctly across all provider backends (local, modal, docker).

# Design principles

1. **Simplicity**: The system should be as simple as possible, both in terms of user experience and internal architecture. Each workspace is simply a web server with some persistent storage (ideally just a file system) that, by convention, ends up calling an AI agent to respond to messages from the user. The only required routes are for the index and for handling incoming messages.
2. **Personal**: Workspaces are designed to serve an *individual* user. They may respond to requests from other humans (or agents), but only to the extent that they are configured to do so by their primary human user.
3. **Open**: Workspaces are both transparent (the user should always be able to see exactly what is going on and dive into any detail they want) and extensible (the user should be able to easily add new capabilities, and to modify or remove existing ones).
4. **Trustworthy**: Workspaces should take security and safety seriously. They should have minimal access to data that they do not need, and for the minimal amount of time that they need it.

# Architecture for workspace agents

Each workspace is created from a template repository (or local directory). The repo's own `.mngr/settings.toml` drives all configuration -- agent types, templates, environment variables, and other settings. There is no `minds.toml`, vendoring, or parent tracking.

Within a workspace, the "primary" agent (carrying `is_primary=true`) is dedicated to running the bootstrap and background services -- its window-0 command is `sleep infinity && claude`, so claude never actually starts there. The user's chat agents are separate `mngr` agents; the bootstrap creates the first one on initial container boot and writes `CLAUDE_CONFIG_DIR` to the host env file so every agent (chat, worktree, worker) shares the services agent's Claude config dir (auth, plugins, marketplaces, sessions). The services agent is hidden from the UI agent list and the system_interface destroy endpoint refuses to tear it down. See [the swap-primary-agent spec](../../../specs/swap-primary-agent/spec.md) for the design rationale.

Some workspace dependencies (currently Playwright's Chromium browser + its apt system libraries) are intentionally installed *after* container boot via the `[program:deferred-install]` section in the FCT `supervisord.conf` (a one-shot `autorestart=false` service), gated by a per-package marker file. This keeps the Docker image build fast: nothing required to start the chat agent or any boot-time service depends on the deferred packages. See the forever-claude-template's `libs/bootstrap/README.md` for the deferral contract.

## Configuration

All configuration lives in the template repository's `.mngr/settings.toml`. The desktop client passes `--template main` plus a mode-specific template (`--template docker` for DOCKER, `--template lima` for LIMA, `--template vultr` for CLOUD, or `--template imbue_cloud` for IMBUE_CLOUD) when running `mngr create`. The template's settings file defines everything the agent needs.

## Data and services

Workspaces use space in the host volume (via the agent dir) for persistent data. The structure and format of this data is up to each individual workspace. You can optionally configure them to store their memories in git (but that is less secure, as data would leak out if synced).

Workspaces *must* serve web requests on one or more ports. On startup, they write JSON records to `$MNGR_AGENT_STATE_DIR/events/services/events.jsonl` -- one line per service -- containing the service name and URL, e.g. `{"service": "web", "url": "http://127.0.0.1:9100"}`. An agent may write multiple records for different services (e.g. a "web" UI service and an "api" backend service). Later entries for the same service name override earlier ones. The desktop client reads this via `mngr event <agent-id> services/events.jsonl` to discover all backends.

# Desktop client

The desktop client handles routing and authentication so that the URLs being served by the workspace are accessible remotely.

See [the desktop client design doc](../imbue/minds/desktop_client/README.md) for more details on how it is implemented.

## Agent creation

When a user visits the desktop client and no agents exist, they are shown a creation form where they can provide a git repository URL or local path. The desktop client:

1. Clones the repository to a temp directory (if a URL) or uses the local path directly
2. Runs `mngr create <name> --no-connect --label workspace=<name> --template main --template <mode>` to create the agent (the agent id is read back from the `created` JSONL event; minds does not pre-generate one)
3. Creates a Cloudflare tunnel (if configured) and injects the tunnel token into the agent via `mngr exec`
4. Redirects the user to the newly created agent (the user is already authenticated via the global session)

Agent creation is also available via the `/api/create-agent` API endpoint, which accepts a JSON body with `git_url` (a URL or local path) and returns the agent ID for status polling.

### Cloudflare tunnel integration

The remote service connector URL comes from the per-tier `client.toml` selected by `minds run --config-file <path>` (see `apps/minds/docs/environments.md`). `minds run` has no implicit default: if neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH` is set it refuses to start. The packaged Electron build passes `--config-file` explicitly from the bundled `client.toml`. Every tunnel request authenticates with the signed-in user's SuperTokens session: the JWT is sent as a Bearer token, and the session's email becomes the default Cloudflare Access policy for new services. No client-side Basic-auth credentials or `OWNER_EMAIL` need to be configured. Once a user is signed in, the desktop client creates a Cloudflare tunnel per new agent that provides global access to the agent's services gated on that user's email.

Within each workspace's dockview UI, a Share action per service opens a modal that surfaces the global Cloudflare link and provides toggle controls for enabling/disabling global forwarding per service.

# Command line interface

- `minds run` (starts the local desktop client for accessing and creating workspaces)

# Deferred items

The following are planned but not in the initial implementation:

- [future] Remote desktop client deployment (e.g. to Modal) for access from anywhere
- [future] Mobile notifications from workspaces
- [future] Desktop client / system tray icon
- [future] Multi-agent interaction between workspaces
- [future] Offline agent handling (serving cached pages when agent is not running)
