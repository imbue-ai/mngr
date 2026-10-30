- The desktop backend's HTTP clients now use `httpx2`, Pydantic's maintained fork of the unmaintained `httpx` with the same API. The package depends on `httpx2>=2.13.1` instead of `httpx`.

- httpx2 verifies TLS against the operating system's trust store. On macOS, that store trusts the compromised emSign chain until Apple's 2026-10-19 update is installed, so the app now sets `SSL_CERT_FILE` to its environment's certifi bundle for the backend. That is the same bundle git and latchkey's curls already use, and httpx2 reads `SSL_CERT_FILE` first. On Linux the backend uses the host's CA store, as latchkey's curls do.

- The dev-time desktop bundle lock (`electron/pyproject/uv.lock`) is regenerated. It had still recorded `imbue-mngr-forward` depending on `httpx`.

- Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.
