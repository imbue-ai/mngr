# Boot Loader Resilience

Make the minds workspace boot process more robust by ensuring the user always has a recovery path when the web server fails to start or begins misbehaving.

## Problem

The current boot sequence has a deep dependency chain. Every service (including the terminal) flows through the bootstrap service manager:

```
container start
  -> mngr creates agent (window 0)
  -> extra_windows creates bootstrap window
  -> bootstrap reads services.toml (5s poll)
  -> bootstrap creates svc-web, svc-terminal, svc-cloudflared, svc-app-watcher
```

If bootstrap crashes, hangs, or services.toml is corrupt, both the web server and the terminal go down together. The user sees an infinite "Loading..." page with no diagnostic information and no way to access the container except via `mngr connect` from their local CLI.

The terminal is the universal debugging tool -- once you have a terminal, you can inspect any tmux window, read logs, restart services, etc. It should not share a failure path with the thing it is meant to debug.

### Failure modes today

| Failure | Web | Terminal | User sees |
|---------|-----|----------|-----------|
| Bootstrap crashes | down | down | Infinite "Loading..." -- no escape hatch |
| services.toml missing/corrupt | down | down | Infinite "Loading..." -- no escape hatch |
| minds-workspace-server bug (5xx) | broken | up | Infinite "Loading..." -- terminal exists but user has no link to it |
| svc-web crashes | down | up | Infinite "Loading..." -- terminal exists but user has no link to it |
| ttyd crashes | up | down | Web works, but no terminal fallback if web breaks later |

In the last three rows, the terminal is available but the user cannot discover it from the loading page.

## Design

Two changes, both required for the full benefit:

1. **Move the terminal out of bootstrap** into an `extra_windows` entry so it starts directly from `mngr create`, with no dependency on bootstrap.
2. **Make the loading page link to available servers** so users can reach the terminal (or any other server) when the web server is unavailable.

### Principle

The terminal is the escape hatch. It must:
- Start independently of every other service
- Be discoverable from the loading page without requiring the web server

After these changes, the failure table improves:

| Failure | Web | Terminal | User sees |
|---------|-----|----------|-----------|
| Bootstrap crashes | down | **up** | Loading page **with terminal link** |
| services.toml missing/corrupt | down | **up** | Loading page **with terminal link** |
| minds-workspace-server bug (5xx) | broken | up | Loading page **with terminal link** |
| svc-web crashes | down | up | Loading page **with terminal link** |
| ttyd crashes | up | down | Web works normally |
| Both crash independently | down | down | Infinite "Loading..." (much less likely) |

## Expected Behavior

### Loading page with fallback links

When the web server is unavailable (backend not registered, returns 5xx, or SSH tunnel fails), the desktop client returns a loading page that:

- Shows "Loading..." with auto-reload every 1 second (unchanged)
- Below the loading message, shows links to other available servers for this agent
- Specifically, if the terminal server is registered, shows a prominent "Open terminal" link
- Links are to the desktop client proxy URLs (e.g., `/agents/{agent_id}/terminal/`), not to raw backend ports
- Links use `target="_top"` so they escape any iframe wrapper (browser info bar)

The available servers are queried from the backend resolver on each page load. Since the page reloads every second, new servers appear within 1 second of registration.

If no other servers are available, the loading page looks the same as today (just "Loading..." with no links).

### Terminal as an independent extra window

The terminal (ttyd) starts as a direct extra window created by `mngr create`, alongside bootstrap and telegram. It no longer depends on bootstrap reading services.toml.

The terminal continues to:
- Use dynamic port allocation (`-p 0`)
- Register itself in `runtime/applications.toml` via `forward_port.py`
- Write server events to `events/servers/events.jsonl`
- Provide the `agent.sh` dispatch script for agent terminal access

The terminal typically starts and registers within 1-2 seconds of agent creation, well before the web server is ready.

## Changes

### Template repo: `forever-claude-template`

**`.mngr/settings.toml`** -- Add terminal to extra_windows in the `main` template:

```toml
[commands.create.templates.main]
extra_windows = {
    bootstrap = "uv run bootstrap",
    telegram = "uv run telegram-bot",
    terminal = "bash scripts/run_ttyd.sh",
    reviewer_settings = "bash scripts/create_reviewer_settings.sh ..."
}
```

**`services.toml`** -- Remove the `terminal` service:

```toml
[services.web]
command = "python3 scripts/forward_port.py --url http://localhost:8000 --name web && minds-workspace-server"
restart = "never"

# terminal service removed -- now an extra_window

[services.cloudflared]
command = "uv run cloudflare-tunnel"
restart = "on-failure"

[services.app-watcher]
command = "uv run app-watcher"
restart = "on-failure"
```

No changes needed to `scripts/run_ttyd.sh` -- it already handles everything independently.

### Monorepo: `apps/minds/` -- Loading page with fallback links

**`proxy.py`** -- Update `generate_backend_loading_html()` to accept optional server links:

The function signature changes from:
```python
def generate_backend_loading_html() -> str:
```
to:
```python
def generate_backend_loading_html(
    agent_id: AgentId | None = None,
    current_server: ServerName | None = None,
    other_servers: tuple[ServerName, ...] = (),
) -> str:
```

When `other_servers` is non-empty, the page includes a section below "Loading..." with links to each server. The terminal link (if present) is shown most prominently.

The parameters are optional with defaults that preserve the current behavior for any call site that does not pass them.

**`app.py`** -- Update the three call sites that return the loading page:

1. `backend_url is None` (line 584)
2. SSH tunnel failure (line 609)
3. Backend 5xx response (line 643)

Each call site already has `parsed_id`, `parsed_server`, and `backend_resolver` in scope. The change is:

```python
# Before
return HTMLResponse(content=generate_backend_loading_html())

# After
other_servers = tuple(
    s for s in backend_resolver.list_servers_for_agent(parsed_id)
    if s != parsed_server
)
return HTMLResponse(content=generate_backend_loading_html(
    agent_id=parsed_id,
    current_server=parsed_server,
    other_servers=other_servers,
))
```

## Edge Cases and Considerations

### Timing: terminal not yet registered when loading page first shown

The loading page reloads every 1 second. Each reload generates fresh HTML with the current set of available servers. If the terminal has not registered yet on the first reload, it will appear on a subsequent reload once ttyd starts and calls `forward_port.py`. The maximum delay is the terminal startup time (typically 1-2 seconds).

### Browser info bar iframe

For non-Electron browsers, the loading page is rendered inside the browser info bar's iframe. Terminal links must use `target="_top"` to navigate the top-level window rather than staying inside the iframe.

### Bootstrap restart policy is a no-op

The current bootstrap service manager stores the `restart` field from services.toml but never uses it -- `_reconcile()` only checks whether a service exists in the desired set, not whether it is actually running. Moving the terminal out of bootstrap does not lose any restart capability because none existed.

### The `agent` sub-URL

`run_ttyd.sh` registers two servers: `terminal` (raw bash shell) and `agent` (attaches to the agent's tmux window 0). Both will appear as links on the loading page. This is intentional -- they serve different purposes, and the user can choose which is more useful for debugging. The `agent` link is particularly valuable because it lets the user see exactly what the AI agent is doing.

### Backward compatibility

The loading page changes are backward-compatible: if no `agent_id` is passed, the page renders identically to today. Existing call sites can be migrated incrementally.

The template repo change (moving terminal to extra_windows) takes effect only for newly created agents. Existing agents continue using the bootstrap-managed terminal until recreated.

### `@pure` decorator

`generate_backend_loading_html()` is currently decorated with `@pure`. This decorator is advisory only (no caching or runtime enforcement). The updated function with parameters remains pure -- same inputs produce the same output -- so the decorator is still appropriate.

## Testing

- **Unit test**: Verify `generate_backend_loading_html()` includes terminal link HTML when `other_servers` contains `ServerName("terminal")`.
- **Unit test**: Verify the loading page contains no extra links when `other_servers` is empty (backward compatibility).
- **Unit test**: Verify links use `target="_top"`.
- **Integration test**: Verify the full proxy path returns a loading page with server links when the backend is unavailable but other servers are registered.

Template repo changes are tested manually by creating a new agent and verifying that the terminal starts independently of bootstrap.

## Future Improvements

These are not part of this spec but are natural follow-ons:

- **Boot progress indicator**: The loading page could show which stage of boot the agent is in (agent starting, bootstrap running, web server starting) by querying agent state from the desktop client API. This would help users distinguish "still starting" from "something broke."
- **Status page**: A dedicated `/agents/{agent_id}/status` page on the desktop client showing agent state, registered servers, and recent events. More useful for ongoing debugging than the loading page fallback.
- **Bootstrap resilience**: Wrap the bootstrap main loop in a try/except so it logs errors and continues rather than crashing the entire service manager. Consider adding actual restart-policy enforcement for bootstrap-managed services.
