# How it works

Each workspace is created from a template repository and lives on one machine -- a container on most providers, the VM itself on Lima -- as a set of persistent `mngr` agents: a primary agent that runs the workspace's background services, plus the chat agents (and any worker or worktree agents) created in it over time. The template defines everything the workspace needs: apps, services, skills, configuration, and a Dockerfile.

## Architecture

The system has two main components:

### Desktop client (runs on your machine)

The desktop client (`minds run`) provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible workspaces (or, when there are none, a Create button). Shutdown-capable machines (the local `docker` / `lima` backends and the cloud `aws` / `gcp` / `azure` / `imbue_cloud` ones) show a live container-status badge and a Start/Stop button (Stop asks for confirmation); the status comes from the discovery snapshot's host state (a user-issued Start/Stop flips it immediately via an optimistic override). The same liveness, narrowed to local machines, drives the quit-time shutdown prompt (see `desktop-app.md`).
- Workspace creation from git repositories or local paths via a web form or API
- Byte-forwarding of HTTP and WebSocket traffic from `[<label>.]agent-<hex>.localhost:8421/*` to the workspace's own backends: the bare origin reaches the system interface (the `system-interface` CLI, source at `default-workspace-template/system/apps/system_interface/`), `<label>.` origins reach the registered service with that origin label (optionally through an SSH tunnel for remote agents)

Each workspace runs its own system interface (the `system-interface` CLI, source at `default-workspace-template/system/apps/system_interface/`), which serves the desktop (windows over a wallpaper, a taskbar, a launcher). The system interface, like every other registered service, owns its own origin, so nothing proxies or rewrites service traffic. The workspace's chat is a registered app (`chat`) at its own origin, and the system interface frames app pages in its windows. Browsers enter a workspace at `https://agent-<hex>.localhost:8421/`, where `agent-<hex>` is the workspace id (its primary agent's id), and page loads there are redirected to the system interface's own origin; each service is at `https://<label>.agent-<hex>.localhost:8421/` (see [Port forwarding](#port-forwarding) for the label).

### Workspace (runs in a container or VM)

Inside each workspace's container (or, on Lima, its VM):
- The **primary agent** (`system-services`) runs only the bootstrap and the background services. It is a plain `command`-type agent whose tmux window 0 runs `sleep infinity`, so no claude is involved; the bootstrap runs in its `bootstrap` window. It is hidden from the UI agent list (it carries `is_primary=true`) and protected against direct destroy.
- The user's **chat agents** are separate `mngr` agents that the chat app creates on demand, each bound to a signed-in provider account; destroying them does not affect the services. A workspace created through the desktop UI opens on the welcome chat its creation page seeds with the conversation so far, and the first message sent there launches the chat's first agent; further chats start from the desktop's launcher.
- The **bootstrap** (`uv run bootstrap`) runs first-boot setup and then execs `supervisord -n`, which supervises the background services, each declared as a `[program:*]` in its own file under `system/supervisord.conf.d/` (pulled in by the `[include]` glob in `system/supervisord.conf`; logs under `/var/log/supervisor`)
- Apps register their ports via `system/scripts/forward_port.py` into `data/.state/apps.toml`
- An **app watcher** service monitors `apps.toml` and writes service events to `events/services/events.jsonl` for discovery
- A **share-gateway** service watches `data/.secrets/share.env` for relay materials and runs the workspace's share stack (relay tunnel + in-workspace TLS) while sharing is enabled
- Users talk to chat agents in the chat app's windows; a chat agent tells the user its work is done by posting to the desktop client's notification feed (`POST /api/v1/agents/<agent_id>/notifications`, through the latchkey gateway)

## Creating workspaces

Workspaces can be created in two ways:

1. **Via the web UI**: On a fresh install the desktop client opens on the first-run start flow, a chat that asks where the first workspace should run (Imbue Cloud, or your own platform through the full create form) and creates it; see [desktop-app.md](./desktop-app.md#the-first-run). Later workspaces come from the home page's Create button and its form: an Imbue Cloud or local preset, backed by an advanced view holding the repository URL (or local path), an optional name, and the compute provider (`IMBUE_CLOUD`, `DOCKER`, `LIMA`, `VULTR`, `MODAL` for testing, or, with `FEATURE_FLAG_BRING_YOUR_OWN_CLOUDS=1`, a bring-your-own-key AWS, GCP, or Azure account). Either way the desktop client clones the repo (if URL) and runs `mngr create` with the appropriate templates, and the creation page shows the attempt's progress. Sharing is machine-level and off by default: the form's "Enable web access" option (which needs a signed-in account) shares the new workspace right after it is created, granting only its owner.

2. **Via the API**: POST to `/api/v1/workspaces` (the endpoint the create form submits to) with a JSON body containing `git_url` and, optionally, `host_name`, `branch`, `launch_mode` (default `DOCKER`), and the form's other fields. The `202` response carries an `operation_id`; poll `/api/v1/workspaces/operations/create/<operation_id>` for progress, which reports the workspace id and a `redirect_url` once the create finishes.

## Port forwarding

Apps (openable as windows on the desktop, with forwarded ports) are tracked in `data/.state/apps.toml`:

```toml
[[apps]]
name = "files"
url = "http://localhost:8300"
label = "files-x7k9q2w1"
```

`label` is the service's origin label, `<name>-<rand>`: minted when the service first registers and kept stable afterwards, so bookmarks and window layouts keep working. Rows registered from an app's `app.toml` manifest also carry the manifest's fields (display name, SVG icon markup, launcher placement, and so on).

Each app gets two URLs:
1. **Local**: `https://{label}.{workspace_id}.localhost:8421/` (the desktop client byte-forwards the service-origin request straight to the registered service's backend)
2. **Shared**: `https://{label}.{workspace_domain}/` (over the workspace's share, while sharing is enabled). A new share's workspace domain is `{share_label}.{user_hash}.{region}.{domain}` (older shares lead with the host id and the unhashed user id instead), and the bare workspace domain does not route.

The Share modal inside the workspace's desktop is authoritative for the actual sharing state.

## Workspace sharing

The remote service connector URL comes from the per-tier `client.toml` loaded via `minds run --config-file <path>` (see `apps/minds/docs/deploy/reference/environments.md`). When neither `--config-file` nor `MINDS_CLIENT_CONFIG_PATH` is set, `minds run` loads the in-repo production `client.toml` (and refuses to start only when `MINDS_ROOT_NAME` names another env without saying where that env's config lives). The packaged Electron build passes `--config-file` explicitly from the bundled `client.toml`. Every share request authenticates with the signed-in user's SuperTokens session -- no Basic-auth credentials or `OWNER_EMAIL` need to be configured on the client.

Sharing is machine-level: when the user enables it for a workspace, the desktop client calls `mngr imbue_cloud shares create` (which registers the share with the connector and returns the share's workspace domain and relay token) and injects those, with the connector and accounts-broker URLs, into the workspace's `data/.secrets/share.env`. The share-gateway service inside the workspace then fetches its relay assignment from the connector, tunnels out to the region's self-hosted relays, obtains a real TLS certificate, and terminates TLS inside the workspace; access is gated by the grants document (`data/.secrets/share_grants.toml`), which the desktop client rewrites in place as the user edits grants. Grants are keyed by account id (an address the owner types is resolved to an account; an unresolved one is an invite the gateway upgrades on first visit), and every request that reaches a workspace service carries the requester's identity in one `X-Imbue-Identity` header (see [design.md](./design.md#request-identity-handed-to-in-workspace-services)).
