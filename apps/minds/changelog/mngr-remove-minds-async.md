Replaced the minds desktop client's FastAPI/asyncio web stack with a synchronous Flask app served by a graceful cheroot WSGI server.

This is an internal framework swap with no user-visible behavior change: every route, path, status code, header, redirect, and Server-Sent-Events stream behaves as before. Notable internals:

- The bare-origin server (`minds run`) now runs on a threaded cheroot WSGI server instead of uvicorn. cheroot speaks HTTP/1.1 with keep-alive (reusing connections) and streams Server-Sent-Events chunk-by-chunk, matching the wire behavior the prior uvicorn server provided -- which the Electron shell depends on (it consumes the one-time login code with a request that 307-redirects and awaits the followed response before loading the UI). Shutdown is unchanged in spirit -- on SIGINT/SIGTERM it flips the shutdown flag and wakes the live SSE streams *before* the server drains, so streams end cleanly with no tracebacks, then closes the HTTP client, stops the discovery/permission consumers, and drains the root concurrency group.

- Server-Sent-Events endpoints (creation logs, the chrome workspace/events stream) are now plain synchronous generators. One unavoidable mechanism change: a browser that closes a stream is noticed on the next write attempt rather than proactively (WSGI exposes no disconnect signal); stream cleanup still runs and there is no functional or UX difference.

- The WebDAV file server under `/api/v1/files` is mounted directly as a WSGI app (the `a2wsgi` ASGI bridge is gone). The `/api/v1` REST API and the `/auth` SuperTokens pages are now Flask blueprints.

- Removed the `fastapi`, `uvicorn`, `a2wsgi`, `python-multipart`, and `websockets` dependencies; added `flask`, `cheroot` (the keep-alive WSGI server), and a direct `werkzeug` dependency (the dispatcher mount and HTTP-exception handling import it directly).
