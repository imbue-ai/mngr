Removed the phantom `global = true` key from the `data/.state/apps.toml`
example in `docs/overview.md` (ticket da-i0go). `system/scripts/forward_port.py`
writes only a service's `name` and `url`, and nothing reads a `global` key, so
the example documented a field that does not exist. Sharing is machine-level, so
there is no per-app global flag to set.
