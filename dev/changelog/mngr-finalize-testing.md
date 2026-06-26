Finalized the minds-workspace Modal snapshot test suite against the latest `main` and confirmed the CI wiring still holds after the merge.

The two-job CI flow (`build-minds-snapshot` builds a fresh `vm_runtime` snapshot image via `scripts/snapshot_minds_e2e_state.py`; `test-minds-snapshot` boots from it via offload's `--override-image-id` and runs the `minds_snapshot_resume` suite) is unchanged and remains behind the `DISABLE_MINDS_SNAPSHOT_CI` kill switch.

The snapshot build script, `offload-modal-minds-snapshot.toml`, the `cleanup_modal_snapshot_images.py` ledger, and the `just test-offload-minds-snapshot` recipe were not affected by the `main` merge (which only touched `apps/minds`); the snapshot build's Electron create flow was re-verified end-to-end against the merged minds frontend.
