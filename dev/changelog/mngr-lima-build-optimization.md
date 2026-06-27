Added the operator-run build + publish pipeline for the pre-baked Lima VM image (issue #2306). These scripts run locally on the release operator's machines (not in CI), so the R2 credentials and the minisign signing key never touch GitHub.

- Rewrote the stale Packer pipeline (`scripts/packer/mngr-lima.pkr.hcl`, `scripts/packer/provision.sh`, `scripts/build-lima-image.sh`) to build a Debian 12 image (matching the Lima provider's base) with the full forever-claude-template toolchain + Playwright baked in by running the exact FCT build scripts, plus cheap reproducibility cleanups for small deltas. Builds one arch per native host (amd64 on a KVM Linux host, arm64 on an Apple-Silicon/HVF Mac) and emits both qcow2 (Lima format) and raw (what gets chunked).

- Added `scripts/lima_image/publish.py`: chunks the raw image with `desync`, signs the per-release root manifest with `minisign`, and uploads the new chunks + index + signed manifest to Cloudflare R2 (via the S3 API or the Cloudflare REST object API). Content-addressed chunks already present are skipped, so re-publishing a near-identical image only uploads the changed chunks.

- Removed the obsolete `scripts/publish-lima-image.sh`.

- Added the implementation spec under `blueprint/lima-image-cache/`.
