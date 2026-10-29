The snapshot-and-shutdown route test uses `httpx2` instead of `httpx`, matching the rest of the repo.

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.
