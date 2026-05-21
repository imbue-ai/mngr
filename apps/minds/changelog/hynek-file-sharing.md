- The ad-hoc `/api/v1/file-server` REST endpoints (GET with
  `operation=READ|LIST|STAT` and POST for writes) have been replaced
  by a standards-compliant WebDAV mount at `/api/v1/files`, backed by
  [`wsgidav`](https://wsgidav.readthedocs.io/) wrapped in
  [`a2wsgi`](https://github.com/abersheeran/a2wsgi). Two share roots
  are exposed:
  - the current user's home directory (`Path.home()`); and
  - `/tmp`.
  Each share is mounted at its on-disk path so the outward URL mirrors
  the absolute path one-to-one: `/home/<user>/foo.txt` is reached at
  `/api/v1/files/home/<user>/foo.txt`, `/tmp/blob.bin` at
  `/api/v1/files/tmp/blob.bin`. Any standard WebDAV verb works
  (`GET`, `PUT`, `PROPFIND`, `DELETE`, ...). Paths outside the two
  shares are not served.
- Authentication is unchanged: the WebDAV mount is gated by the same
  per-agent `Authorization: Bearer <api_key>` check that protects the
  rest of `/api/v1/...`. WsgiDAV itself is configured for anonymous
  access; a thin ASGI wrapper verifies the bearer token against
  `find_agent_by_api_key` and 401s before any request can reach the
  filesystem. The HTML directory browser is disabled.
