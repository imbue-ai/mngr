Added the `minds_snapshot_resume` test suite and reconciled it with the latest `main` (which had merged the error-reporting/Sentry work into the minds desktop client). This consolidates the snapshot-test work from #2226 and #2275; see those PRs for the development history.

`apps/minds/test_snapshot_resume.py` exercises a sandbox booted from a minds-workspace snapshot:

- asserts the snapshot captured a `docker stop`ped FCT workspace container in a clean `exited` state;

- a `running_workspace` fixture resumes it (docker-start the container, restart the system-services agent so the bootstrap respawns its services) and asserts that `system_interface` serves HTTP, the system-services agent is alive, and the core services (system_interface, web, terminal) re-register in `runtime/applications.toml`;

- `test_minds_recovery_restores_dead_system_interface` drives minds' real recovery flow end-to-end: it breaks the workspace (stops system-services), confirms minds' in-container recovery probe diagnoses `system_interface` as unhealthy, performs minds' surgical restart, and confirms recovery.

The shared Electron e2e create-flow driver (`imbue/minds/desktop_client/e2e_workspace_runner.py`) -- used by both the `minds_electron` acceptance test and the snapshot build script -- now streams the Electron renderer's console output, JS errors, and failed requests into the run log, so a renderer-side fault during workspace creation is diagnosable from CI output instead of being hidden behind Electron's main-process-only stderr.

The create flow was re-verified end-to-end against the merged frontend (Electron launch, advanced-config create form, FCT Docker workspace build + start, `system_interface` render); no selector or flow changes were required, and the `test_ratchets.py` snapshots were reconciled with the merge.
