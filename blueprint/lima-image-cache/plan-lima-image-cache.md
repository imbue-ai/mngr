# Pre-baked Lima VM image, distributed via desync CDC deltas

Tracking issue: https://github.com/imbue-ai/mngr/issues/2306

## Overview

- Today the Lima path boots a stock Debian 12 genericcloud qcow2, then runs the full FCT toolchain build (`setup_system.sh` + `install_dependencies.sh` + `build_workspace.sh`) plus `deferred_install.sh` (Playwright/Chromium) *inside the VM on every create*. minds budgets a cold create at ~600s. The goal is to bake that work into a per-release VM image so a local Lima create drops to roughly VM boot + cloud-init (~30-60s; boot is the remaining floor, not zero).
- **Pre-bake a Lima-compatible qcow2 per `minds-v<version>` release, for both arm64 and amd64**, with the FCT toolchain, Playwright/Chromium, vendored mngr, and the default FCT repo's workspace build (at the pinned tag) all baked in. The image stays generic per release (the user's repo is still injected at create time).
- **Distribute via content-defined chunking (`desync`)** so upgrading between versions downloads only changed chunks, seeded from the previously-installed image. Store a content-addressed chunk store + per-(version, arch) index + a minisign-signed root manifest; host on Cloudflare R2 behind the Cloudflare CDN (kept swappable behind a small abstraction).
- **Build + publish via a single local operator-run script** (build + chunk + sign + upload), not CI/tag-triggered, so the R2 credentials and minisign private key never touch GitHub and test environments are easy to target. Each arch is built natively (arm64 on an Apple Silicon/HVF host, amd64 on a KVM-enabled Linux host) and uploads its own arch directly into the R2 chunk store (desync skips chunks already present remotely, so uploads are naturally incremental).
- **minds prefetches the image at startup and gates Lima create on readiness**, but only for the "default" workspace (current release tag + default FCT repo URL, non-dev-loop). The Lima provider needs no code change to consume the image — minds points it at the local assembled qcow2 via the existing `providers.lima.default_image_url_*` config override. Anything else (other tag/branch, custom repo, dev loop, kill-switch env var, or a version absent from the CDN) falls back to today's build-in-VM path.

## Expected behavior

- **Fast default Lima create.** When the user creates a workspace with the current release tag + default FCT repo and the image is already present locally, `mngr create` boots the baked image; the in-VM provisioning `command -v … || install` guards short-circuit and the per-create workspace build hits warm caches, so create completes in roughly boot + cloud-init time instead of ~600s.
- **Prefetch runs in the background from app launch.** A minds backend worker thread starts on boot and begins ensuring the current image is present (download base, or seed-delta upgrade from the prior local image), writing progress to a per-env state file. By the time a user clicks create, the image is usually already downloaded.
- **Create blocks on a usable-but-not-ready image, showing progress.** If the user creates before prefetch finishes (and the gate conditions hold), create waits on the download/apply and surfaces progress in the existing creation UI — this is faster than rebuilding. It does not fall back to building while a valid download is in flight.
- **Idempotent base-or-upgrade, resumable.** "Ensure current image present" is safe to re-run: no base → download base; old base → fetch needed chunks (seeded by the old image) and assemble the new one; the old version is deleted only after the new one is fully assembled and verified. Interrupted downloads resume rather than restart.
- **Retention keeps only the current version.** After a verified upgrade, the previous local image is deleted.
- **Graceful when a version isn't published.** If the CDN has no manifest/data for the selected tag+arch, minds warns and proceeds with the current build-in-VM path (the cache is simply not available for that version yet).
- **Retryable hard-fail when a published version's download breaks.** For a version that *does* exist on the CDN, a download/verify failure (network drop, bad signature, insufficient disk) does not silently rebuild the slow way: the gated create fails with a clear error. Retries happen at two levels — inner automatic retries with backoff (plus background auto-retry while the app is open), and an outer manual click-to-retry once inner retries are exhausted; resumable downloads let an offline-then-online user continue.
- **Integrity + authenticity.** Chunk hashes give integrity; minisign verification of the root manifest (against a public key baked into the shipped binary) ensures the assembled image is a genuine `minds-v<version>` build before it is ever used.
- **Non-default paths unchanged.** Other tags/branches, custom repos, the dev loop (rsync'd local `vendor/mngr`), and any provider other than Lima behave exactly as today. A kill-switch env var disables the baked-image path entirely (forces build-in-VM) for testing/dev.
- **Isolation across envs.** Each minds env stores its own image cache under its own data root; envs never share image state, so test/staging/production stay independent and free of cross-env races.

## Changes

### Image bake (creation side)

- Rewrite the stale Packer pipeline (`scripts/build-lima-image.sh`, `scripts/packer/mngr-lima.pkr.hcl`, `scripts/packer/provision.sh`, `scripts/publish-lima-image.sh`) — currently Ubuntu 24.04, thin package set, predates the docker-in-VM removal, unreferenced.
- New bake target produces a **Debian 12 bookworm** Lima-compatible qcow2 (matching the provider's current base) that still has working cloud-init + the Lima guest agent.
- Bake the full FCT toolchain by running the release tag's `setup_system.sh` + `install_dependencies.sh` + `build_workspace.sh` + `deferred_install.sh` (Playwright/Chromium) inside the image, plus the vendored mngr, so the provider's provisioning guards short-circuit at create.
- Bake the **default FCT repo's workspace build at the pinned `minds-v<version>` tag** so the per-create build hits warm package caches (apt/npm/uv/pip/Playwright) and prebuilt artifacts. The image remains generic per release (no user repo baked).
- Build each arch natively: arm64 on an Apple Silicon host (HVF), amd64 on a KVM-enabled Linux host (or operator-controlled cloud VM).
- Apply cheap reproducibility cleanups before imaging (normalize mtimes via `SOURCE_DATE_EPOCH`, wipe `/var/cache`, drop `.pyc` and apt/dpkg logs, etc.) to trim delta tails; full bit-for-bit reproducibility is not required (CDC tolerates block shift).

### Distribution (CDC + hosting)

- Standardize on **`desync`** (single static binary; native HTTP/S3/local chunk stores) as the CDC tool.
- Chunk the **raw** image (not the qcow2, whose metadata churn amplifies diffs); deliver qcow2 as the Lima format but chunk the raw form.
- Publish artifacts: a content-addressed **chunk store**, a **per-(version, arch) index**, and a **signed root manifest** mapping `minds-v<version>` + arch → index hash + expected final image hash + minisign signature.
- Sign the root manifest with **minisign (Ed25519)**, detached; private key held only on the operator's publish machine, public key baked into the shipped minds binary.
- Host on **Cloudflare R2 origin + Cloudflare CDN**, behind a small storage abstraction so the origin/CDN stays swappable.
- Upload by chunking directly into the R2 chunk store as the desync target; desync skips chunks already present remotely (content-addressed), with a local chunk-existence cache to avoid re-HEADing — so uploads are incremental without a separate local mirror.

### Local build + publish workflow

- A single end-to-end local operator-run script: build → chunk → sign → upload, run where the secrets live. Not GitHub Actions, not tag-triggered.
- Operator runs it once per arch on the corresponding native host; each invocation builds and uploads its own arch.
- Document the procedure in `apps/minds/docs/release.md` as a release step (decoupled from the `minds-v<version>` tag push itself).

### minds consumer side

- New "ensure current image present" capability (idempotent base-or-upgrade, resumable, verify-before-swap, retention = keep current only) living in the minds desktop client, with its manifest/chunk-store base URL and trusted public key sourced from **per-env config** (default = production CDN; staging/test/e2e point at a fixture origin).
- New backend **prefetch worker thread started on boot** that drives the ensure-image operation, with inner auto-retry/backoff + background auto-retry while the app is open, and writes progress/status to a per-env state file under the env data root (e.g. `~/.minds(-<env>)/lima-images/`).
- Bundle the `desync` binary alongside the existing `uv`/`git`/`lima` resources for the desktop app, and add it to the child-process `PATH`.
- **Gate the Lima create flow** (`agent_creator.py` LIMA path) on image readiness when the gate conditions hold: launch mode Lima + template ref == current release tag (`FALLBACK_BRANCH`) + repo URL == default FCT URL (`_FALLBACK_GIT_URL`) + not the dev loop + kill-switch env var unset. When ready, point Lima at the local assembled qcow2 via `providers.lima.default_image_url_aarch64`/`_x86_64`; the stock Debian URL remains the non-gated fallback.
- Create-flow fallbacks: version absent on CDN → warn + build-in-VM; gated download in progress → block with progress UI; gated download of a published version fails → retryable hard-fail (manual retry surfaced in the creation UI).
- Surface prefetch/download progress through the existing creation status phases (`AgentCreationStatus`), and add a manual retry affordance once inner retries are exhausted.
- Add a kill-switch env var that disables download + use of the baked image entirely (forces build-in-VM).
- Clean up the stale docker-in-VM comments the issue flags in the minds onboarding/agent-creator code while in the area.
- Add a free-disk preflight before download/assembly; treat insufficient space as a retryable hard-fail rather than a silent rebuild.

### Validation (early phase)

- Before investing further in reproducibility, measure real `desync` delta sizes across two consecutive real builds, then decide how far to push the cheap reproducibility cleanups.

### Cross-cutting

- Changelog entries per touched project (`libs/mngr_lima` if touched, `apps/minds`, and `dev` for `scripts/` + docs).
- No `mngr_lima` provider code change is required to *consume* the image (config override already exists); changes there, if any, are limited to constants/docs.

✓ Explore  ✓ Plan  ● Write  ○ Refine
