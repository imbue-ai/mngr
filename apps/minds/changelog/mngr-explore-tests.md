Expanded the `minds_snapshot_resume` suite (run by the snapshot CI job from a pre-built workspace image) beyond the single stopped-container sanity check:

- A `running_workspace` fixture resumes the captured workspace (docker-start the container, restart the system-services agent so the bootstrap respawns the services) and asserts that `system_interface` serves, the system-services agent is alive, and the core services (system_interface, web, terminal) re-register in `runtime/applications.toml`.

- `test_minds_recovery_restores_dead_system_interface` drives minds' real recovery flow: it breaks the workspace (stops system-services), confirms minds' in-container recovery probe diagnoses `system_interface` as unhealthy, performs minds' surgical restart, and confirms recovery.
