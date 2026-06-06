Hardened suspicious edge-case handling in the mngr e2e test harness
(`imbue/mngr/e2e/conftest.py`): a malformed `~/.modal.toml` now fails loudly
when loading Modal credentials instead of silently substituting empty tokens;
a missing `PATH` is no longer defaulted to an empty string; the redundant
`FileNotFoundError` in the Modal-environment cleanup catch was dropped; and the
best-effort process-teardown handlers (asciinema pid reading, liveness checks,
SIGINT delivery) now carry comments explaining why each catch is correct.
