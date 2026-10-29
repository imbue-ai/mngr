The connector client's requests now use `httpx2`, Pydantic's maintained fork of the unmaintained `httpx` with the same API. The package depends on `httpx2>=2.13.1` instead of `httpx`. httpx2 verifies TLS against the operating system's trust store instead of certifi's bundle, unless `SSL_CERT_FILE` names a bundle.

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.
