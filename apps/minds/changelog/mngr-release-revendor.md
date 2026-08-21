`just sync-vendor-mngr` now regenerates default-workspace-template's root `uv.lock` and commits it with the vendor snapshot, matching the `sync_vendor` CI job.

The template's root `uv.lock` pins the vendored mngr libraries as editable path dependencies and records their resolved dependencies, so a snapshot that moves any of them strands it. Every template build path installs with `uv sync --frozen`, which takes the lock as the source of truth without checking it against the manifests, so the workspace venv silently gets the previous dependency set.

The relock writes the template root's own lock, never the `system/vendor/mngr/uv.lock` inside the snapshot, so the vendor-match invariant that the release flow checks in step 6 is unaffected.

`docs/deploy/release.md` and `docs/vendor-mngr-sync.md` describe the added step.
