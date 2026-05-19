Latchkey gateway ships a new bundled `minds-api-proxy` extension that
transparently reverse-proxies requests under `/extensions/minds-api-proxy`
to the minds desktop client's bare-origin "Minds API". The upstream URL
is read at request time from the `LATCHKEY_EXTENSION_MINDS_API_URL`
environment variable, and is published to the detached
`mngr latchkey forward` supervisor (via the new
`LatchkeyForwardSupervisor.extra_env`) on every `minds run` startup, so
the proxy always points at the live Minds API port even when minds
re-binds on restart. The extension responds 503 when the env var is not
configured; requests still go through the gateway's normal permission
check.

The Minds REST API ships a new `/api/v1/file-server` endpoint for
reading, listing, stat-ing, and writing files on the desktop host:

* `GET /api/v1/file-server?path=<absolute>&operation=READ` streams a
  file's bytes back to the caller.
* `GET /api/v1/file-server?path=<absolute>&operation=LIST` returns a
  JSON directory listing with per-entry type, size, and mtime.
* `GET /api/v1/file-server?path=<absolute>&operation=STAT` returns
  the same metadata for a single path (files, directories, and
  symlinks classified via `lstat`).
* `POST /api/v1/file-server?path=<absolute>` writes the raw request
  body to disk. Defaults to refusing with `409 Conflict` when the
  target already exists; pass `overwrite=true` to replace an existing
  regular file. Missing parent directories are created on demand.

The endpoint uses the same per-agent Bearer-token authentication as
the rest of `/api/v1/` and is reachable from agents through the
`minds-api-proxy` Latchkey extension.
