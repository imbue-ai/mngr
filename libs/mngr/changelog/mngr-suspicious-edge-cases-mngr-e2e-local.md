Hardened suspicious edge-case handling in the mngr e2e test harness
(`imbue/mngr/e2e/conftest.py`): Modal-credential loading now delegates to the
shared `load_active_modal_credentials` helper (in `imbue.mngr_modal.testing`),
which fails loudly on a malformed `~/.modal.toml` instead of silently
substituting empty tokens; a missing `PATH` is no longer defaulted to an empty
string; the redundant `FileNotFoundError` in the Modal-environment cleanup catch
was dropped; `_is_pid_alive` now catches `PermissionError` specifically (EPERM
means the process is alive) rather than all `OSError`; the SIGINT-delivery loop
now ignores only the expected already-gone process (`ProcessLookupError`) and
logs any other `os.kill` failure instead of silently swallowing it; and the
best-effort process-teardown handlers (asciinema pid reading, liveness checks,
SIGINT delivery) now carry comments explaining why each catch is correct.
