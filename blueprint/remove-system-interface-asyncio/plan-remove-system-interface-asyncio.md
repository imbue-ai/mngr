# Remove asyncio from the system interface (FastAPI -> Flask)

> Work happens in the `forever-claude-template` repo, on branch `mngr/remove-workspace-async`
> (worktree at `.external_worktrees/forever-claude-template/`). The `blueprint/` plan itself lives in the monorepo.

## Refined prompt

we want to remove all asyncio usage from the system interface inside of the forever-claude-template that we use as the default template for the Minds app.
In order to do this, we'll need to switch to using flask instead of FastAPI (and use the flask-sock library for any websockets)

The point is to simplify, and to completely remove asyncio from the forever-claude-template (because coding agents are bad at dealing with async programs)

* Scope covers the served web stack -- `apps/system_interface`, the shared `libs/web_server` placeholder lib, and the `build-web-service` skill's scaffolding -- plus standalone copyable reference snippets (e.g. `claude_p.py` in `use-ai-integration`), all rewritten to plain sync so no async patterns ship anywhere in the template.
* Do a single hard cutover of `system_interface` (server, dispatcher, broadcaster, endpoints, tests) in one PR -- no FastAPI/Flask coexistence and no backward-compatibility layer.
* Serve the Flask app via the Werkzeug threaded dev server (`run_simple(..., threaded=True)`, single process, no connection cap -- fine for single-workspace scale), replacing `uvicorn`; keep the `system-interface` console-script entrypoint.
* Build all app-wide objects eagerly in the `create_application` factory and tear them down via `atexit` plus a SIGTERM/SIGINT handler (replacing the FastAPI lifespan and its SIGINT-only handler).
* Replace `app.state.*` with a single typed context object (frozen config plus the mutable service handles) attached to the Flask app and read via `current_app`.
* Treat thread-safety as part of this task: audit `AgentManager`, the `watchers` dict, and the latchkey cache for concurrent access under the threaded server and add locking where needed.
* Replace the WS broadcaster's async-task-cancellation and the proxy's `asyncio.wait` teardown with thread-per-connection isolation; detect dead WS peers via flask-sock's built-in `ping_interval` keepalive.
* Use `simple-websocket` (already transitive via flask-sock) as the sync WebSocket client for the proxy's backend leg, replacing the async `websockets` library.
* Retype the `endpoint` plugin hook to a Flask app and rewrite the `build-web-service` scaffolding to emit Flask + flask-sock services, with no FastAPI back-compat shim.
* Preserve byte-for-byte error-response parity (catch-all via Flask `errorhandler(Exception)`; identical status codes and `{"detail": ...}` envelopes).
* Target no frontend changes (identical SSE frames and WS message JSON); allow small matching frontend edits only as a fallback for backend-framework edge cases -- most likely WS close-code/reconnect handling in `AgentManager.ts` and proxy WS subprotocol/close behavior.
* Port HTTP tests to Flask's `app.test_client()`; exercise WS/SSE endpoints against a real `run_simple` listener booted in a background thread on an ephemeral port (plain integration tests, no marker); keep the Playwright e2e suite as-is.
* Keep `httpx` (sync `httpx.Client`) as the HTTP client for the service proxy and latchkey.

---

## Overview

* **Goal:** eliminate every `asyncio` / ASGI construct from the `forever-claude-template`'s served web stack by replacing FastAPI/uvicorn with Flask + flask-sock, so the code coding-agents must edit is plain synchronous Python.
* **Why:** the template is the default deploy target for the Minds app; coding agents that work inside it are unreliable with async control flow. Removing it simplifies the mental model and makes agent edits safer.
* **Shape of the change:** the FastAPI app is already mostly sync (route bodies run in a threadpool, SSE uses a sync generator, the broadcaster is thread/queue-based). The async surface is concentrated in 5 files; this is a framework swap plus a true-threading hardening pass, not a logic rewrite.
* **Concurrency model shift:** uvicorn's single event loop becomes Werkzeug's thread-per-connection. This is the main behavioral risk -- handlers that were loop-serialized now run truly concurrently -- so shared mutable state gets a thread-safety audit, and the bespoke async-cancellation machinery for wedged sockets is deleted in favor of thread isolation + flask-sock ping keepalive.
* **Hard cutover, no back-compat:** one PR swaps the framework wholesale; the frontend wire contract (REST + SSE + WS message JSON) stays identical so the UI needs no changes by default.

## Expected behavior

* The system interface serves the same UI at the same host/port, with the same `system-interface` console-script entrypoint and the same `supervisord` `system_interface` program command (`... && system-interface`).
* All REST endpoints return identical JSON bodies and status codes (`200/201/400/403/404/409/500/502/503/504`), including the catch-all `{"detail": "Internal server error: ..."}` 500.
* SSE streams (`/api/agents/{id}/stream`, subagent stream) emit byte-identical frames, including `: keepalive` comments; `EventSource` clients are unaffected.
* WebSocket endpoints (`/api/ws`, `/api/proto-agents/{id}/logs`, `/service/{name}/{path}`) speak the same message JSON and the same initial snapshot order (`agents_updated` -> `applications_updated` -> `proto_agent_created`).
* Dead/half-open WS clients are reaped by flask-sock `ping_interval` keepalive instead of the old consecutive-queue-full task-cancellation; an unresponsive client no longer wedges anything because each connection owns its thread.
* The `/service/<name>/...` proxy forwards HTTP (incl. SSE streaming) and bidirectional WebSockets exactly as before, including subprotocol passthrough, cookie-path scoping, HTML rewriting, and the SW bootstrap / loading-page flows.
* Layout-broadcast loopback semantics are unchanged: mutex contention still returns `409` with holder metadata; `list`/`inspect`/`refresh`/`reload_system_interface` still bypass the mutex; terminal-panel ref allocation still returns synchronously.
* Claude-auth and latchkey endpoints behave identically (same providers, same status/error mapping, OAuth subprocess still survives between `/start` and `/submit-code`).
* On `SIGTERM` (supervisord stop) or `SIGINT`, the server tears down broadcaster, watchers, agent manager, and HTTP clients cleanly -- broader and more reliable than today's SIGINT-main-thread-only path.
* The `web` service placeholder and any newly-scaffolded services (`build-web-service`) are Flask apps; the proxy treats them no differently.
* No async patterns remain anywhere in the template: a repo-wide search for `asyncio` / `async def` / `await` / `fastapi` / `uvicorn` / ASGI in shipped (non-vendored) code returns nothing.

## Changes

### `apps/system_interface` -- framework swap
* Replace FastAPI/uvicorn/starlette/`websockets` with Flask + flask-sock + `simple-websocket`; drop those dependencies from `pyproject.toml` and add the new ones.
* `main.py`: keep argparse (`--provider/--include/--exclude`); replace `uvicorn.run(...)` with a threaded Werkzeug serve (`run_simple(host, port, app, threaded=True)`).
* `server.py` (`create_application`): return a Flask app; register routes via Flask route decorators/`add_url_rule` instead of `add_api_route`; convert every `async def` handler and `await request.json()/.body()` to sync (`request.get_json()` / `request.data`), and inline the former `run_in_threadpool(...)` calls as direct sync calls.
* Replace `app.state.*` with one typed context object (frozen config + mutable service handles: broadcaster, agent manager, watchers registry, layout mutex, http clients, filters) attached to the app and read via `current_app`.
* Replace the FastAPI `lifespan` with eager construction in the factory plus teardown wired through `atexit` and a `SIGTERM`/`SIGINT` handler.
* Replace the catch-all `@app.exception_handler(Exception)` with a Flask `errorhandler(Exception)` that produces the identical envelope.
* Serve static assets / `index.html` / favicon / plugin scripts through Flask (`send_file` / `send_from_directory`), preserving the meta-tag injection (base path, hostname, agent id, plugin tags).
* SSE endpoints: return a Flask streaming response wrapping the existing sync generator; keep the keepalive/shutdown-sentinel behavior.

### `apps/system_interface` -- WebSockets
* `ws_broadcaster.py`: keep the thread-safe queue/lock design; delete the asyncio `_handler_by_id` task-capture and `loop.call_soon_threadsafe(task.cancel)` eviction path. Dead-client handling relies on per-connection threads + flask-sock ping keepalive (retain or simplify the queue-overflow drop as a backpressure guard, without async cancellation).
* `/api/ws` and proto-agent-logs handlers: convert to flask-sock handlers that block directly on `client_queue.get(timeout=...)` and `ws.send(...)` in their own thread (no threadpool hop); preserve the initial-snapshot sequence and disconnect handling.
* `service_dispatcher.py`: convert `httpx.AsyncClient` -> sync `httpx.Client` (including `client.stream(...)` for SSE passthrough); replace the async `websockets` backend client with `simple-websocket`'s sync client; bridge the two WS directions with two threads coordinated by a shared stop flag (the sync analog of `asyncio.wait(FIRST_COMPLETED)` + cancel), closing both sockets when either side ends.
* `claude_auth_endpoints.py` / `latchkey_endpoints.py`: convert handlers to plain sync (latchkey is already sync); drop `run_in_threadpool`; keep status/error mapping and the `_on_auth_success` welcome-resend chokepoint.
* `hookspecs.py`: retype the `endpoint(app: FastAPI)` hook to receive a Flask app; update the `register_event_broadcaster` wiring as needed.

### `apps/system_interface` -- thread safety
* Audit `AgentManager`, the `watchers` dict (mutated in `_get_or_create_watcher`), and the latchkey catalog cache for concurrent access under the threaded server; add locking (or other synchronization) where shared mutable state can be touched by multiple request threads at once.

### Shared lib + skills + snippets
* `libs/web_server/runner.py`: rewrite the placeholder from FastAPI/uvicorn to Flask + threaded Werkzeug; keep `/` and `/health`, the `WEB_SERVER_PORT` env, and `main()`; drop the FastAPI/uvicorn deps; update the docstring's "FastAPI/ASGI" wording.
* `.agents/skills/build-web-service/`: rewrite `scaffold_fastapi_lib.py` (and its docs/templates) to scaffold Flask + flask-sock services; rename as appropriate.
* `.agents/skills/use-ai-integration/scripts/claude_p.py`: rewrite from `anyio`/`async def`/`to_thread.run_sync` to plain sync; sweep for any other copyable snippets using async and convert them.

### Tests
* Port HTTP/SSE tests from FastAPI `TestClient` to Flask `app.test_client()`.
* Add a shared fixture that boots `run_simple` in a background thread on an ephemeral port for WS/SSE real-listener tests (`/api/ws`, proto-agent logs, service-proxy WS); plain integration tests, no marker.
* Update `ws_broadcaster_test.py` and `service_dispatcher_test.py` for the de-async'd implementations.
* Keep the Playwright `test_e2e.py` suite as-is (it already drives a real server); verify it still passes against the threaded Werkzeug server.
* Update `test_ratchets.py` / any import-linter layered contract for the new module shape; add (or rely on existing) checks that fail if `asyncio`/`fastapi`/`uvicorn`/`async def` reappear.

### Bookkeeping
* Frontend: no changes by default; only if a backend-framework edge case (WS close-code/reconnect in `AgentManager.ts`, or proxy WS subprotocol/close behavior) makes a small frontend tweak cleaner than backend emulation.
* Changelog entries in the `forever-claude-template` repo for each touched project (`apps/system_interface`, `libs/web_server`, and the agent-skill/dev changes), per that repo's changelog conventions.
