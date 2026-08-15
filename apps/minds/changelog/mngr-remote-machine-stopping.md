imbue_cloud workspaces gain the desktop Start/Stop control (the same gate as local docker/lima workspaces): Stop halts the slice VM and uploads it to the tier's storage bucket, freeing the paid bare-metal slot; Start restores it. The start timeout now accommodates the restore-from-storage path.

New per-tier workspace-storage secret (`.minds/template/storage.sh`, pushed as the `storage-<env>` Modal secret via the `deploy.toml` services list); dev envs share the dev tier's bucket with an automatic per-env key prefix. Provisioning runbook: `docs/workspace-stop-start.md`. The dev tier's Vault entry is populated (bucket `mngr-workspaces-dev`).

Plans gain `max_total_workspaces` (running + stopped; explorer 10 / ally 50) alongside `max_remote_workspaces` (running only).

New end-to-end deployment test `deployment_tests/test_workspace_stop_start.py` exercises the full lease -> stop -> start -> release cycle against a real env (skips cleanly without a baked slice or storage config).

Clicking a stopped imbue_cloud workspace no longer auto-dispatches a start through the recovery flow (whose command ceiling is sized for local container bounces): the tile shows a message telling the user to press Start, since a cloud restore can take several minutes. Local docker/lima workspaces keep the existing click-to-start behavior.

`minds env destroy` now deletes the env's workspace stop/start artifacts from the tier's storage bucket (the env's stamped key prefix for dev/ci envs, the whole keyspace for shared tiers), so destroyed envs no longer orphan paid storage. Skipped when the tier's `storage` Vault entry is unpopulated.
