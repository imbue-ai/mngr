Slice-fleet cutover phase 2 (mngr-internal `new-fleet-phase-2`):

- `deployment_tests/test_machine_migration.py` is deleted with the in-supervisor gen-1 -> gen-2 conversion it exercised; the cutover is proven by the rehearsal checklist in `blueprint/slice-fleet-cutover`.

- `deployment_tests/test_machine_resize.py` no longer needs `MINDS_MACHINE_RESIZE_RELEASE_TEST=1`; it runs whenever the minds release tier runs. The stop/start test keeps its opt-in until the CI boxes are gen-2 (cutover phase 5).

- Docs: `host-pool-setup.md` explains that the backup snapshot relies on the data disk's 4 GiB system reserve (a workspace at quota can still be snapshotted) and why `host_backup` deletes its snapshot as soon as restic has read it; `next_deploy.md` gains the checklist item for connector migrations 034-039 (039 requires deploying the bake and the connector together); `workspace-stop-start.md` drops the retired lazy `disk_gb` backfill.

- `deployment_tests/test_analytics_collection.py` stamps `memory_units` / `disk_gb` on the placeholder `pool_hosts` row it inserts, since migration 039 makes both columns NOT NULL.
