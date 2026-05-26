# Test-efficiency groundwork: offload v0.9.6 + minds e2e snapshot script

Two changes that together lay the groundwork for much faster minds
end-to-end tests:

- Bumped the offload CI pin from `0.9.5` to `0.9.6` (`.github/workflows/ci.yml`).
  v0.9.6 adds `offload run --override-image-id <ID>`, which lets us point
  offload at a pre-built Modal image and skip the entire image-setup
  pipeline (Modal provider only). See
  https://github.com/imbue-ai/offload/releases/tag/v0.9.6 for the full
  release notes.
- Added `scripts/snapshot_minds_e2e_state.py`, a demonstration script that
  creates a Modal sandbox with `experimental_options={"vm_runtime": True}`,
  installs the Docker + Node + pnpm + xvfb stack the
  `test-docker-electron` CI job needs, runs the existing minds Electron
  e2e test (which creates a workspace's Docker container as a side
  effect), and then calls `sandbox.snapshot_filesystem()` to capture the
  state. The resulting Modal image ID can be fed back to offload via
  `--override-image-id` so future test runs boot from an already-warm
  workspace + Docker container in seconds instead of rebuilding from
  scratch every time. The script intentionally opts in to `vm_runtime`
  only for itself -- Modal has capacity issues with that runtime, so we
  do not flip it on for the general mngr_modal provider.
