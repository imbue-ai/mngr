- New direct dependencies recorded in `uv.lock` to support the minds
  WebDAV file-server mount: `wsgidav` (the WebDAV server itself) and
  `a2wsgi` (the WSGI-to-ASGI adapter that bridges it onto Starlette /
  FastAPI). Both are pulled in via `apps/minds/pyproject.toml`.
