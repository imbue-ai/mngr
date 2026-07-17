`launch_to_msg_e2e.py` now honors a `LATCHKEY_ENCRYPTION_KEY` environment variable in `latchkey_env()`, matching the key-resolution precedence of `load_or_create_encryption_key` (the env-var override wins over the per-directory `encryption_key` file).

Previously `latchkey_env()` hard-required the file and raised `E2EFailure` when it was absent, which wrongly failed a runner that supplies the key via the env var -- e.g. a headless CI mac runner with no reachable login keychain, where setting the override deliberately suppresses the file's creation.
