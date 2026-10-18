---
name: imbue-studio
description: Use whenever Imbue Studio, minds, a workspace, the studio API, or an Imbue account comes up. Explains how to reach Imbue Studio's API from the user's machine or from inside a workspace, how to start the app when it is not running, where the live contract lives, and what an agent must never do with the user's credentials.
---

# Driving Imbue Studio

Imbue Studio (the `minds` app) runs persistent agents in workspaces and exposes one versioned HTTP API for managing them.
The API is self-describing.
Always read the live contract before assuming a route or a field, and prefer it over anything written here:

```bash
GET /api/schema
```

The same API is reached two ways, depending on where you are running.
Decide first, then use that transport for every call.

## 1. Where am I?

**Inside a workspace** (`LATCHKEY_GATEWAY` is set in your environment): go through the latchkey gateway's proxy with `latchkey curl`, never plain `curl`.
The gateway injects the API credential; you never hold it.

```bash
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/schema
latchkey curl http://latchkey-self.invalid/minds-api-proxy/api/v1/workspaces
```

**On the user's own machine** (no `LATCHKEY_GATEWAY`): talk to the running app over its Unix domain socket.
The socket is authenticated by the operating system (only processes running as the user can connect), so there is no token to find or store.

```bash
curl --unix-socket "$SOCKET" http://localhost/api/schema
curl --unix-socket "$SOCKET" http://localhost/api/v1/workspaces
```

## 2. Finding the socket

Imbue Studio keeps one state directory per tier.
The tier is named by `MINDS_ROOT_NAME`; when it is unset the tier is production and the root name is `minds`.
Any other root name has the form `minds-<tier>`.

| Platform | State root |
|---|---|
| macOS | `~/Library/Application Support/Imbue Studio/<tier>/` (`production` for the default) |
| Linux | `~/.minds/` for production, `~/.minds-<tier>/` otherwise |

While the app runs, the state root holds `app.json` and, normally, `api.sock`.
Read `app.json` first when it exists: it names the real `socket_path` (which can live elsewhere when the state root's path is too long for a socket) and the `launch.argv` command that starts this install.
Without `app.json`, assume `<state root>/api.sock`.

```bash
ROOT="$HOME/Library/Application Support/Imbue Studio/production"   # macOS, production
SOCKET=$(jq -r .socket_path "$ROOT/app.json" 2>/dev/null || echo "$ROOT/api.sock")
```

## 3. Starting the app when it is not running

A connection refused, or no socket file, means the app is not running.
Start it in the background; it shows no window and the user's shortcut still opens one later.

- With `app.json` present: run its `launch.argv` exactly as given.
  It returns immediately and leaves the app running detached from your shell.
- Without `app.json`: on macOS `open -g -n -a "Imbue Studio" --args --background`; on Linux `imbue-studio --background` for a package install, or the AppImage file with `--background`; from a source checkout, `uv run minds run --no-browser` from the repo root.

Then wait for readiness by polling the version route; a packaged launch syncs its environment first, so allow up to about two minutes:

```bash
until curl -sf --unix-socket "$SOCKET" http://localhost/api/v1/app/version >/dev/null; do sleep 2; done
```

Running `launch.argv` while the app is already running is harmless; the running instance ignores it.

## 4. Reading the contract

`GET /api/schema` is an OpenAPI document generated from the routes actually registered, and it differs per transport: over the socket it lists everything the desktop UI can do, over the gateway only what the proxy can reach.
Use it to find paths, request bodies, and response shapes, and trust it over any example here.

Long operations (create, destroy, restart) answer `202` with an operation handle; poll the operation route the schema names until it reports done, and read its `/logs` stream when something fails.
A `400` with `{error, field}` is a field-level problem; a `422` with `{"errors": [...]}` is a structurally invalid body.

Routes under `/ui/api/` are the desktop UI's own and are not versioned.
They may change without notice; use the versioned routes the schema lists whenever one exists.

## 5. Permissions inside a workspace

Over the gateway, most routes are gated per verb and the user approves each grant.
A `403` saying the request was not permitted means you need a grant, not that the route is wrong.
File exactly one permission request per tool call, with nothing else in that call and its output untouched, then end your turn and wait for the user's answer before retrying:

```bash
latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": "'"${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"'", "type": "workspace", "payload": {"permissions": ["minds-workspaces-read"], "target_workspace_id": null}, "rationale": "I need to list your workspaces to find the one you mentioned."}'
```

Ask for the smallest set of verbs the task needs, name the target workspace for targeted verbs, and explain why in plain language.
Over the socket there is no permission step; you act as the user.

## 6. Never

- Never ask for, read, or send the user's credentials, API keys, or master password.
  Sign-ins happen in the user's browser; when a route starts one, hand the user the URL it returns and poll for completion.
- Never configure backups with user secrets: leave every `backup_*` field unset when creating a workspace, and let the user enable backups in the app.
- Never approve or deny your own permission requests, and never try to reach the desktop's cookie-only routes from inside a workspace.
- Never talk to the user about sockets, gateways, or proxies unless they ask; talk about their workspaces.

## 7. The layers underneath

Imbue Studio is built on mngr, and the lower layers have their own live help:

- Agents and hosts: `mngr help`, and `mngr ask "<question>"` for plain-language answers.
- Imbue Cloud accounts, machines, buckets, and shares: `mngr imbue_cloud --help`; a LiteLLM proxy key for an agent comes from `mngr imbue_cloud keys litellm create`.
- The latchkey gateway that workspaces reach services through: `mngr latchkey --help`.

Prefer the studio API for anything about workspaces as the user sees them, and reach for mngr directly only for what the API does not cover.
