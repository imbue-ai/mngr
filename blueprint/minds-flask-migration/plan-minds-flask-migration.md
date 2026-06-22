# Migrate the minds desktop client from FastAPI/asyncio to Flask

Remove all asyncio and FastAPI usage from the minds **desktop client**
(`apps/minds/imbue/minds/desktop_client/` + `imbue/minds/cli/run.py`) and
switch to **Flask** served by a graceful Werkzeug WSGI server. This is a
strict-parity framework swap: same routes, path params, status codes,
headers, redirect semantics, and SSE wire format, with no user-visible
behavior change beyond the one unavoidable SSE-disconnect mechanism.

## Overview

- The desktop client's async layer is a thin veneer over fundamentally
  synchronous, thread-based code (`ConcurrencyGroup`, subprocesses, queues).
  Async handlers mostly just `await request.json()/form()` then call sync code,
  and blocking work is already pushed off the event loop via
  `run_in_executor(None, fn, ...)`. Flask's synchronous, thread-per-request
  model fits this code far better and removes the async indirection.
- There are **no websockets** in the desktop client. Agent-subdomain HTTP/WS
  forwarding moved out to the `mngr_forward` plugin, and the system_interface
  (in forever-claude-template) owns its own WS stack — both out of scope. So
  `flask-sock` is not added; there is nothing in scope to wire it to.
- The server becomes a small, explicitly-managed graceful Werkzeug WSGI server
  with one ordered, testable shutdown sequence — replacing both the uvicorn
  `_PreShutdownAwareServer` subclass and the large async `_managed_lifespan`
  teardown. Clean, fast shutdown (no tracebacks, no "strand did not finish"
  warnings) is a first-class, tested requirement.
- The only intentional behavior change: WSGI cannot proactively detect a
  client disconnect, so the chrome-events SSE drops `request.is_disconnected()`
  and instead ends the stream on the next failed write plus the shutdown flag.
  Generator cleanup (`finally` removing resolver/health callbacks) still runs.
- Now-unused dependencies (`fastapi`, `uvicorn`, `a2wsgi`, `python-multipart`,
  `websockets`) are removed and `flask` is added; WsgiDAV (already a WSGI app)
  mounts directly via Werkzeug, dropping the `a2wsgi` ASGI bridge.

## Expected behavior

- Every existing route responds identically: same paths, `<agent_id>`-style
  params, status codes, headers, redirects, HTML/JSON bodies, and the
  `text/event-stream` wire format (`data: ...\n\n`, keepalives, `X-Accel-Buffering`).
- `minds run` starts the bare-origin server on the same host/port, prints the
  same login URL and emits the same JSONL events (`mngr_forward_started`,
  `login_url`), and opens the browser exactly as before.
- The two SSE streams (creation logs at `/api/create-agent/{id}/logs`, chrome
  events at `/_chrome/events`) deliver the same events in the same order.
- On SIGINT/SIGTERM the server flips the shutdown flag and wakes SSE loops
  before draining, so streams end cleanly; the process exits quickly with no
  asyncio/anyio cancellation tracebacks and no concurrency-group warnings.
- SSE behavior change (only observable difference): a browser that closes a
  stream is noticed on the next write attempt (bounded by the keepalive /
  re-assert cadence) rather than instantly; no functional or UX regression.
- `/api/v1/...` (telegram, notifications), the WebDAV mount under
  `/api/v1/files`, the `/auth/...` SuperTokens pages/APIs, and `/_static`
  assets all behave exactly as today, including the central-key Bearer auth.
- Tests assert the same behaviors through Flask's test client instead of
  starlette's, plus a new check that shutdown is fast and clean and that SSE
  generator cleanup runs.

## Changes

### Web framework
- Replace the FastAPI app built in `desktop_client/app.py` (`create_desktop_client`,
  keeping the same name/signature and returning a Flask app) with a Flask app.
- Convert all `async def` route handlers and SSE generators to synchronous
  functions; replace `await request.json()/form()` with Flask's sync request
  parsing and `run_in_executor(None, fn, ...)` with direct calls (in
  `app.py`, `api_v1.py`, `supertokens_routes.py`, `request_handler.py`, and the
  latchkey grant/deny handlers `latchkey/handlers/predefined.py` and
  `latchkey/handlers/file_sharing.py`).
- Convert the FastAPI routers (`api_v1.py`, `supertokens_routes.py`) to Flask
  Blueprints registered at the same prefixes; convert `{param}` paths to
  `<param>`.
- Replace FastAPI response types (`HTMLResponse`, `StreamingResponse`,
  `FileResponse`, `RedirectResponse`, `Response`, `HTTPException`) with Flask
  equivalents (`Response`, streaming response, `send_file`, `redirect`/explicit
  Location, `abort`), preserving status codes and headers.
- Replace the global `@app.exception_handler(Exception)` with a Flask
  error handler that logs and returns the same 500 body.

### Dependency injection / app state
- Replace the 138 `request.app.state.*` references with a single typed state
  object stored on the Flask app, accessed via a `get_state(current_app)` helper.
- Replace the 3 FastAPI `Depends` (auth store, backend resolver, central-key
  auth) with direct `current_app` lookups and one auth decorator for the
  `/api/v1/...` central-key Bearer check.

### SSE streaming
- Convert both SSE generators to sync generators wrapped in
  `stream_with_context`; drive cross-thread wakeups with `threading.Event` /
  `queue.Queue` directly (no event loop / `call_soon_threadsafe`).
- Drop `request.is_disconnected()`; end streams on write failure plus the
  shared shutdown flag, keeping the existing `finally` callback cleanup.

### Server, startup, and shutdown
- Add a new `desktop_client/server.py` containing a graceful Werkzeug WSGI
  server (`make_server`, threaded, daemon worker threads) and a
  `desktop_client_runtime(...)` context manager that owns startup and one
  ordered teardown sequence (set shutdown flag, notify the resolver to wake
  SSE, stop the server, close the HTTP client, terminate the envelope and
  permission consumers, stop the prewarmed mngr caller, drain the root
  concurrency group).
- Install own SIGINT/SIGTERM handlers that run the pre-drain ordering before
  stopping the server.
- Slim `cli/run.py` to build dependencies and delegate serving/teardown to the
  new module; remove the uvicorn import and the `_PreShutdownAwareServer`
  subclass. Move the startup geo-detection kickoff and all teardown out of the
  deleted async lifespan into the runtime context manager.

### Mounts and static assets
- Mount the existing WSGI WebDAV app directly via Werkzeug dispatch middleware
  under `/api/v1/files`, dropping `a2wsgi`; convert the ASGI Bearer-auth gate in
  `webdav.py` to an equivalent WSGI middleware.
- Serve `/_static` via Flask's static handling at the same URL from the same
  on-disk directory.

### Dependencies
- Remove `fastapi`, `uvicorn`, `a2wsgi`, `python-multipart`, and `websockets`
  from `apps/minds/pyproject.toml` (after verifying each is unreferenced); add
  `flask`.

### Tests
- Migrate all `starlette`/`fastapi` `TestClient` usage to Flask's
  `test_client()` (e.g. `mind_controls_test.py`, `providers_panel_test.py`,
  `permission_routes_test.py`, `webdav_test.py`, and the root
  `test_sse_redirect.py`, `test_desktop_client_e2e.py`), preserving assertions.
- Add a shared SSE test helper that reads N events from a Flask streaming
  response with a timeout, and route SSE tests through it.
- Add a plain (unmarked) integration test that starts the real server, opens an
  SSE connection, sends SIGTERM, and asserts the process exits quickly and
  cleanly and that the SSE generator's cleanup ran.

### Electron and changelog
- Update the stale "uvicorn's graceful exit" comment in `electron/main.js` and
  regenerate `electron/pyproject/uv.lock`.
- Add a changelog entry under `apps/minds/changelog/` and a `dev/changelog/`
  entry for the Electron/lock change.
