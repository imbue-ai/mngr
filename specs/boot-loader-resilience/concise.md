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

Four changes that reinforce each other:

1. **Move the terminal out of bootstrap** into an `extra_windows` entry so it starts directly from `mngr create`, with no dependency on bootstrap.
2. **Use a fixed, known-by-convention port for ttyd** instead of dynamic allocation. This eliminates the port-detection machinery in `run_ttyd.sh` and makes the terminal URL predictable.
3. **Always show the terminal link on the loading page**, unconditionally -- even before the terminal has registered with the backend resolver. The link goes through the desktop client proxy, so if the terminal is not yet ready, the user gets the terminal's own auto-retrying loading page (which resolves within 1-2 seconds). This is dramatically better than no link at all.
4. **Also show links to other available servers** that the backend resolver knows about, for completeness.

### Principle

The terminal is the escape hatch. It must:
- Start independently of every other service
- Be discoverable from the loading page without requiring the web server
- Not depend on a successful registration chain to be linkable

After these changes, the failure table improves:

| Failure | Web | Terminal | User sees |
|---------|-----|----------|-----------|
| Bootstrap crashes | down | **up** | Loading page **with terminal link** |
| services.toml missing/corrupt | down | **up** | Loading page **with terminal link** |
| minds-workspace-server bug (5xx) | broken | up | Loading page **with terminal link** |
| svc-web crashes | down | up | Loading page **with terminal link** |
| ttyd crashes | up | down | Web works normally |
| Both crash independently | down | down | Loading page with terminal link (link shows terminal's own loading page until ttyd recovers) |

## Expected Behavior

### Loading page with fallback links

When the web server is unavailable (backend not registered, returns 5xx, or SSH tunnel fails), the desktop client returns a loading page that:

- Shows "Loading..." with auto-reload every 1 second (unchanged)
- **Always** shows a "Terminal" link and an "Agent" link below the loading message, unconditionally. These are convention-based links (`/agents/{agent_id}/terminal/` and `/agents/{agent_id}/agent/`) that will work as soon as ttyd has started, regardless of whether the backend resolver has discovered the terminal server yet. If the terminal is not yet ready, clicking the link shows the terminal's own auto-retrying loading page, which resolves within 1-2 seconds.
- Additionally shows links to any other servers the backend resolver knows about (excluding the server currently being loaded)
- Links are to the desktop client proxy URLs, not to raw backend ports
- Links use `target="_top"` so they escape any iframe wrapper (browser info bar)

### Terminal as an independent extra window

The terminal (ttyd) starts as a direct extra window created by `mngr create`, alongside bootstrap and telegram. It no longer depends on bootstrap reading services.toml.

The terminal uses a **fixed, known-by-convention port** (7681, ttyd's default) instead of dynamic allocation. This:
- Eliminates the stderr-parsing port detection logic in `run_ttyd.sh`
- Makes `forward_port.py` registration immediate (the URL is known ahead of time)
- Simplifies the script significantly

The terminal continues to:
- Register itself in `runtime/applications.toml` via `forward_port.py`
- Write server events to `events/servers/events.jsonl`
- Provide the `agent.sh` dispatch script for agent terminal access

The terminal typically starts within 1-2 seconds of agent creation, well before the web server is ready.

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

**`scripts/run_ttyd.sh`** -- Simplify to use a fixed port:

The script changes from dynamic port allocation (`-p 0` with stderr parsing) to a fixed port:

```bash
TTYD_PORT=7681
python3 "$REPO_ROOT/scripts/forward_port.py" --name terminal --url "http://localhost:$TTYD_PORT"
# Also register the agent sub-URL and write events.jsonl entries (same as before, but with known port)
exec ttyd -p "$TTYD_PORT" -a -t disableLeaveAlert=true -W bash -c "$DISPATCH_SCRIPT"
```

The port registration and event writing happen *before* starting ttyd (since the port is now known), and `exec` replaces the shell with ttyd for cleaner process management. The stderr-parsing `while IFS= read -r line` pipeline is removed entirely.

### Monorepo: `apps/minds/` -- Loading page with fallback links

**`proxy.py`** -- Update `generate_backend_loading_html()` to accept an agent ID and additional server links:

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

When `agent_id` is provided, the page **always** includes "Terminal" and "Agent" links (convention-based, unconditional). When `other_servers` is non-empty, links to those additional servers are also shown. The current server being loaded is excluded.

The parameters are optional with defaults that preserve the current behavior for any call site that does not pass them.

**`app.py`** -- Add a `_make_loading_html()` helper and update the three call sites that return the loading page:

1. `backend_url is None`
2. SSH tunnel failure
3. Backend 5xx response

A private helper encapsulates the logic of querying the backend resolver and building the loading page:

```python
def _make_loading_html(
    agent_id: AgentId,
    server_name: ServerName,
    backend_resolver: BackendResolverInterface,
) -> str:
    other_servers = tuple(
        s for s in backend_resolver.list_servers_for_agent(agent_id)
        if s != server_name
    )
    return generate_backend_loading_html(
        agent_id=agent_id,
        current_server=server_name,
        other_servers=other_servers,
    )
```

Each call site already has `parsed_id`, `parsed_server`, and `backend_resolver` in scope. The change at each site is:

```python
# Before
return HTMLResponse(content=generate_backend_loading_html())

# After
return HTMLResponse(content=_make_loading_html(parsed_id, parsed_server, backend_resolver))
```

The `terminal` and `agent` links are rendered even if they are not in `other_servers` (they are unconditional when `agent_id` is provided). The `other_servers` tuple provides links to any additional servers beyond the convention-based ones.

## Edge Cases and Considerations

### Terminal link before terminal is ready

The terminal and agent links are shown unconditionally (whenever `agent_id` is known). If the user clicks the link before ttyd has started, they land on the terminal's own loading page which auto-retries every second. Since ttyd starts in 1-2 seconds as an extra window, the wait is brief. This is intentionally better than hiding the link: a loading page that resolves quickly is far more useful than no link at all.

### Fixed port conflicts

Using a fixed port (7681) means ttyd will fail to start if another process is already on that port. In practice, each agent runs in its own container, so conflicts are unlikely. If a conflict does occur, ttyd exits with a clear error visible in the terminal's tmux window, which is easier to diagnose than a silently-assigned random port.

### Browser info bar iframe

For non-Electron browsers, the loading page is rendered inside the browser info bar's iframe. Terminal links must use `target="_top"` to navigate the top-level window rather than staying inside the iframe.

### Bootstrap restart policy is a no-op

The current bootstrap service manager stores the `restart` field from services.toml but never uses it -- `_reconcile()` only checks whether a service exists in the desired set, not whether it is actually running. Moving the terminal out of bootstrap does not lose any restart capability because none existed.

### Backward compatibility

The loading page changes are backward-compatible: if no `agent_id` is passed, the page renders identically to today (no terminal link, no server links). Existing call sites can be migrated incrementally.

The template repo change (moving terminal to extra_windows and switching to a fixed port) takes effect only for newly created agents. Existing agents continue using the bootstrap-managed terminal until recreated.

### `@pure` decorator

`generate_backend_loading_html()` is currently decorated with `@pure`. This decorator is advisory only (no caching or runtime enforcement). The updated function with parameters remains pure -- same inputs produce the same output -- so the decorator is still appropriate.

## Testing

- **Unit test**: Verify `generate_backend_loading_html(agent_id=..., ...)` always includes terminal and agent links when `agent_id` is provided.
- **Unit test**: Verify the loading page contains no links when `agent_id` is `None` (backward compatibility).
- **Unit test**: Verify additional servers from `other_servers` appear as links.
- **Unit test**: Verify links use `target="_top"`.
- **Integration test**: Verify the full proxy path returns a loading page with terminal link when the backend is unavailable, even before the terminal server has registered.

Template repo changes are tested manually by creating a new agent and verifying that the terminal starts independently of bootstrap on the fixed port.

## Future Improvements

These are not part of this spec but are natural follow-ons:

- **Boot progress indicator**: The loading page could show which stage of boot the agent is in (agent starting, bootstrap running, web server starting) by querying agent state from the desktop client API. This would help users distinguish "still starting" from "something broke."
- **Status page**: A dedicated `/agents/{agent_id}/status` page on the desktop client showing agent state, registered servers, and recent events. More useful for ongoing debugging than the loading page fallback.
- **Bootstrap resilience**: Wrap the bootstrap main loop in a try/except so it logs errors and continues rather than crashing the entire service manager. Consider adding actual restart-policy enforcement for bootstrap-managed services.
