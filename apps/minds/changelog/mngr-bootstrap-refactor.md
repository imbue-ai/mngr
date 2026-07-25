Refactored the minds bootstrap layer (`imbue/minds/bootstrap.py`) into a minimal pre-mngr-import env-root module plus a new `imbue/minds/mngr_settings/` package holding all mngr settings.toml machinery (reconciliation, provider enable/disable, imbue_cloud account blocks, bring-your-own-key cloud accounts, and legacy migrations).

Settings reconciliation is now declarative (desired blocks as data with pinned vs user-owned fields) and runs from `main.py` before any mngr import, with a runtime guard and import-linter contracts enforcing the ordering. Legacy cleanups (ssh provider block, ambient aws region blocks, dynamic_hosts artifacts) are quarantined in `mngr_settings/_migrations.py`.

All settings writes now serialize on an inter-process file lock, fixing a last-writer-wins race between signin, startup reconciliation, and providers-panel toggles.

Behavior change: a set-but-invalid `MINDS_ROOT_NAME` (e.g. a stale `devminds`) now fails every `minds` command with a clean one-line error instead of silently falling back to production; run `unset MINDS_ROOT_NAME` and re-activate. `is_minds_root_name_set_to_active_env()` is renamed `is_env_activated()`.

The dev-env-name pattern is now defined once in the bootstrap module (suffix bound unified at `{0,34}`); `environments.md` and the desktop-client README were corrected (the client is Flask, not FastAPI), along with library-layer log levels (info -> debug).
