Reconciled the `minds_snapshot_resume` test suite with the latest `main` (which merged the error-reporting/Sentry work into the minds desktop client) so the snapshot CI keeps passing.

The shared Electron e2e create-flow driver (`imbue/minds/desktop_client/e2e_workspace_runner.py`) -- used by both the `minds_electron` acceptance test and `scripts/snapshot_minds_e2e_state.py` -- was verified end-to-end against the merged frontend: it still launches Electron, drives the (advanced-config) create form, builds and starts the forever-claude-template Docker workspace, and waits for `system_interface` to render. No selector or flow changes were required.

The `minds_snapshot_resume` suite (`test_snapshot_resume.py`) and the recovery probe it exercises are unchanged; `main` brought no `libs/mngr` or FCT-contract changes, so the resume assertions (services-agent liveness, `applications.toml` re-registration, surgical recovery) continue to hold.
