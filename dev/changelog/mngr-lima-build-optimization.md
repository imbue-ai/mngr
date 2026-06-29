Added the operator-run build + publish pipeline for the pre-baked Lima VM image (issue #2306). These scripts run locally on the release operator's machines (not in CI), so the R2 credentials and the minisign signing key never touch GitHub.

- Replaced the stale Packer/QEMU image pipeline with a Lima-based bake (`scripts/build-lima-image.sh` + `scripts/lima_image/bake_provision.sh`; removed `scripts/packer/`). Baking with Lima means the image is produced by the same virtualizer that consumes it (`vz` on Apple Silicon, accelerated QEMU on Linux), so the artifact is guaranteed Lima-bootable and the macOS build host needs no separate QEMU/Packer toolchain. It boots the Debian 12 base (matching the Lima provider), runs the exact forever-claude-template build scripts (`setup_system`/`install_dependencies`/`build_workspace` + Playwright) to bake the toolchain, applies cheap reproducibility cleanups for small deltas, then flattens the Lima disk to a standalone qcow2 (Lima format) + raw (what gets chunked). Builds one arch per native host (amd64 on a KVM Linux host, arm64 on an Apple-Silicon Mac).

- Added `scripts/lima_image/publish.py`: chunks the raw image with `desync`, signs the per-release root manifest with `minisign`, and uploads the new chunks + index + signed manifest to Cloudflare R2 (via the S3 API or the Cloudflare REST object API). Content-addressed chunks already present are skipped, so re-publishing a near-identical image only uploads the changed chunks.

- Removed the obsolete `scripts/publish-lima-image.sh`.

- Added the implementation spec under `blueprint/lima-image-cache/`.
