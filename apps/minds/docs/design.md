# Overview

See the [README](../README.md) for an overview of what workspaces are and see [the glossary](./workspace/glossary.md) for terminology used throughout.

# Relationship to mngr

Workspaces are built on top of `mngr` and should interact with it exclusively through the `mngr` CLI interface. Workspaces should never directly access mngr's internal data directories (e.g., `~/.mngr/agents/`). Instead, use `mngr` commands like `mngr list`, `mngr event`, `mngr exec`, etc. This ensures workspaces remain compatible as mngr's internals evolve and work correctly across all provider backends (local, modal, docker).

The one exception is the desktop's avatar (the default workspace template's `docs/system/blueprint/pinned-taskbar-entries/plan-pinned-taskbar-entries.md`): the shell reads the agents event file the `mngr observe` process writes at `$MNGR_HOST_DIR/events/mngr/agents/events.jsonl` to show whether any agent is working. It is a read of a documented, append-only event log under the standard event envelope, not of an agent's internal state; the shell parses it as plain JSON, imports nothing from mngr, and never runs `mngr` (whose startup cost and per-host process management the always-on avatar could not afford, and whose CLI has no subscription the shell could hold). The conventions it relies on (the path, the three event types, the `is_primary` label, the two running states) are duplicated as constants beside a note of this exception, and a change to any of them degrades to a stale or idle avatar rather than an error.

# Design principles

1. **Simplicity**: The system should be as simple as possible, both in terms of user experience and internal architecture. Each workspace is simply a web server with some persistent storage (ideally just a file system) that, by convention, ends up calling an AI agent to respond to messages from the user. The only required routes are for the index and for handling incoming messages.
2. **Personal**: Workspaces are designed to serve an *individual* user. They may respond to requests from other humans (or agents), but only to the extent that they are configured to do so by their primary human user.
3. **Open**: Workspaces are both transparent (the user should always be able to see exactly what is going on and dive into any detail they want) and extensible (the user should be able to easily add new capabilities, and to modify or remove existing ones).
4. **Trustworthy**: Workspaces should take security and safety seriously. They should have minimal access to data that they do not need, and for the minimal amount of time that they need it.

# Architecture for workspace agents

Each workspace is created from a template repository (or local directory). The repo's own `.mngr/settings.toml` drives all configuration -- agent types, templates, environment variables, and other settings. There is no `minds.toml`, vendoring, or parent tracking.

Within a workspace, the "primary" agent (carrying `is_primary=true`) is dedicated to running the bootstrap and background services -- it is a plain `command`-type agent whose window-0 command is `sleep infinity`, so no claude is ever involved. The user's chat agents are separate `mngr` agents created on demand: the workspace opens on the welcome chat the creation page seeded with the onboarding conversation, whose first agent is launched by the first message sent there (further chats start from the desktop's launcher). Since `minds-v0.5.0` the workspace keeps one config dir per signed-in provider account under `~/.minds/accounts/<id>/`, and a chat is bound to one on its create (an `--env CLAUDE_CONFIG_DIR=<account dir>` for claude, a credential symlink for the other harnesses); `~/.claude` holds no credential. Which account and harness an unqualified create gets is the workspace's own decision: its chat app writes the default account's `type` and binding into `.mngr/settings.local.toml`, mngr's local config layer, so every `mngr create` there that names neither -- the two chats this app starts from outside (`skill_chat.py`), workers, automations -- resolves the same account a chat started from the launcher would, and a workspace with no account signed in refuses the create in its own words. (Workspaces from `minds-v0.5.0` through `v0.5.2` keep accounts but write no such file; for their one update the app falls back to asking the template's `system/scripts/default_account_args.py`.) The services agent is hidden from the UI agent list and the system_interface destroy endpoint refuses to tear it down. See [the swap-primary-agent spec](../../../specs/swap-primary-agent/spec.md) for the original split's design rationale (its shared-config-dir mechanism has since been superseded by the per-account config dirs above).

Some workspace dependencies (currently Playwright's Chromium browser + its apt system libraries) are intentionally installed *after* container boot via the `[program:deferred-install]` section in the DEFAULT_WORKSPACE_TEMPLATE `supervisord.conf` (a one-shot `autorestart=false` service), gated by a per-package marker file. This keeps the Docker image build fast: nothing required to start the chat agent or any boot-time service depends on the deferred packages. See the default-workspace-template's `system/libs/bootstrap/README.md` for the deferral contract.

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
2. Runs `mngr create system-services@<host> --new-host --no-connect --label workspace_display_name=<name> --label is_primary=true --template main --template <mode>` to create the workspace host and its primary agent (the agent id is read back from the `created` JSONL event; minds does not pre-generate one)
3. Redirects the user to the newly created agent (the user is already authenticated via the global session)

Agent creation is also available via the `/api/create-agent` API endpoint, which accepts a JSON body with `git_url` (a URL or local path) and returns the agent ID for status polling.

### Workspace sharing

The remote service connector URL comes from the per-tier `client.toml` selected by `minds run --config-file <path>` (see `apps/minds/docs/deploy/reference/environments.md`). When neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH` is set, `minds run` loads the in-repo production `client.toml` (and refuses to start only when `MINDS_ROOT_NAME` names another env without saying where that env's config lives). The packaged Electron build passes `--config-file` explicitly from the bundled `client.toml`. Every share request authenticates with the signed-in user's SuperTokens session (the JWT is sent as a Bearer token). No client-side Basic-auth credentials or `OWNER_EMAIL` need to be configured.

Sharing is per-workspace and user-initiated: nothing sharing-related happens at create time. When the user enables sharing for a workspace, the desktop client registers a share with the connector (`mngr imbue_cloud shares create`) and injects the relay coordinates + relay token into the workspace, whose share-gateway then dials the self-hosted relay and terminates TLS inside the workspace. Within each workspace's desktop, a Share action opens a modal that surfaces the shared link and edits the grants controlling who may access it.

#### Request identity handed to in-workspace services

A workspace service learns who is making a request from one header, `X-Imbue-Identity`, set the same way whether the request arrives over the relay (the share-gateway) or over the local desktop forward (`mngr forward`). Its value is a compact JSON object:

- `owner` -- always present, `true` or `false`. Over the local forward the single authenticated user is always the owner, so it is always `true`.
- `user_id` and `email` -- the requester's account id and email, present only while the workspace is shared and the entry point knows the requester's account: the share-gateway always knows a visitor's (and the owner's, over the relay), and the desktop knows the owner's for the shared workspaces of its signed-in accounts. An unshared workspace's requests carry only the owner flag.

So exactly two forms leave the desktop: `{"owner":true}` and `{"owner":true,"user_id":"...","email":"..."}`. The header carries no display name or profile picture; whoever needs a profile fetches it from the connector by `user_id`.

The desktop owns this contract (`desktop_client/forward_identity.py`); `mngr forward` knows nothing of it. The desktop writes the header into the proxy's generic per-agent request-headers file at `<data_dir>/forward_headers.json` (passed as `--request-headers-file`): a `"*"` entry stamping `{"owner":true}` on every workspace, plus one entry per shared workspace carrying its owning account's `user_id` and `email` from the plugin's session. Which workspaces are shared comes from the sync service's records listing (`GET /sync/records` reports `shared_agent_ids` beside the records), applied after every workspace-record sync pass, and from this desktop's own share enable/disable, applied immediately so the local view is right without waiting for the next pass. The proxy re-reads the file whenever it changes, strips any client-supplied copy of the header, and stamps the workspace's entry on every forwarded request and WebSocket handshake.

Both entry points strip any client-supplied copy of the header before injecting the authoritative value, so a workspace page cannot forge it. Nothing about the owner is delivered out-of-band anymore: an app that needs to know who is here reads it from requests (see the presence store in the default-workspace-template's `system_interface`). The gateway's own contract is documented in the template's `system/services/share_gateway/README.md`, and the design in [`specs/share-identity-and-presence/spec.md`](../../../specs/share-identity-and-presence/spec.md).

Who may visit is decided by the workspace's grants file (`data/.secrets/share_grants.toml`): `users` (account ids, matched first), `emails` (invites the gateway upgrades to account ids on the invitee's first visit), and `email_domains`. The desktop resolves a typed address to an account when the owner adds it, so a grant survives the grantee changing their email.

# Command line interface

- `minds run` (starts the local desktop client for accessing and creating workspaces)

# Deferred items

The following are planned but not in the initial implementation:

- [future] Remote desktop client deployment (e.g. to Modal) for access from anywhere
- [future] Mobile notifications from workspaces
- [future] Desktop client / system tray icon
- [future] Multi-agent interaction between workspaces
- [future] Offline agent handling (serving cached pages when agent is not running)
