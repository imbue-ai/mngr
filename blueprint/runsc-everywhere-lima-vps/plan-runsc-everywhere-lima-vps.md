# Plan: Run Lima + VPS Docker providers under gVisor (runsc), and slim the Lima VM OS

## Refined prompt

went want to convert both the lima provider and the vps_docker provider in the minds app for our forever-claude-template to be configured to use gvisor (runsc) (ie, by changing the forever-claude-template/.mngr/settings.toml file correctly)

* Scope the runsc conversion to the Lima, OVH, and Vultr providers, plus the `imbue_cloud` template's slow/rebuild path.
* Drive everything from configuration flags in settings.toml rather than modifying the providers; only add a new config passthrough where one doesn't already exist (the Lima inner container's `docker run` args).
* Turn runsc on via the existing `install_gvisor_runtime` option plus `docker_runtime="runsc"`; no backwards-compatibility handling for already-baked hosts.
* Also apply `--security-opt=no-new-privileges` alongside runsc on both vps_docker (via template `start_arg`) and Lima (via the new config passthrough).
* Add a general-purpose "extra container run args" config field to the Lima provider, and set it in FCT to carry `--workdir=/` + `--security-opt=no-new-privileges`.
* Existing pre-baked `imbue_cloud` pool hosts may stay non-runsc until their next bake; no migration/drain required.
* For OVH and Vultr, add `[providers.ovh]` and `[providers.vultr]` blocks to FCT settings.toml carrying `docker_runtime` + `install_gvisor_runtime`.
* Land the `mngr_lima` code change and FCT settings.toml edits in this work; the `vendor/mngr` sync into FCT happens separately via the normal release-minds flow.

We simultanesouly want to convert the lima provider to use a different, much lighter operating system now that everything important is just happening inside of docker (in the lima VM)
ie, rather than a full ubuntu, we ought to be able to get away with something much lighter (alpine? some simpler debian?)
Ideally we end up making it similar to whatever we're using in the ovh provider...

* Use Debian 12 "bookworm" genericcloud as the lighter Lima image, by changing the `mngr_lima` library default constants (so every Lima user benefits) rather than only overriding in FCT.
* Pin the Lima Debian image to a specific dated genericcloud snapshot URL (amd64 + arm64) rather than the `latest/` symlink.
* Apply the Debian swap to both Lima modes (direct-in-VM and docker-in-Lima); apt handles the toolchain on Debian the same as on Ubuntu.
* Validate by requiring a manual one-off Lima + VPS bring-up under runsc/Debian before considering the work done (unit coverage for the new plumbing plus CI acceptance/release tests still apply).

---

## Overview

- gVisor (`runsc`) support already exists in code for both providers (`docker_runtime` + `install_gvisor_runtime` on `LimaProviderConfig` and `VpsDockerProviderConfig`, with working install blocks). This work *enables* it, it does not build it.
- The only missing capability is on Lima: the inner agent `docker run` accepts only `--runtime`, so there is no config path to pass the `--workdir=/` that runsc requires (the image WORKDIR `/mngr/code` lives inside the mounted `/mngr` volume, and runsc aborts when its process cwd already exists). We add one general-purpose config field to close that gap.
- Everything else is configuration: turn on `install_gvisor_runtime` + `docker_runtime="runsc"` per provider, and add the hardening `docker run` args (`--workdir=/`, `--security-opt=no-new-privileges`) via the existing vps_docker `start_arg` path and the new Lima passthrough.
- The Lima VM OS moves from Ubuntu 24.04 to a pinned Debian 12 "bookworm" genericcloud image, changed at the `mngr_lima` library default so all Lima users get the lighter, OVH-matching base; this applies to both Lima modes.
- FCT changes are settings-only; the `mngr_lima` code change is small and config-driven. The vendored-mngr sync into FCT is out of scope here (handled by the normal release-minds flow), so FCT settings will reference the new field once that sync lands.

## Expected behavior

- Lima hosts created from FCT (docker-in-Lima mode) run the agent container under gVisor: the VM provisions `runsc` at first boot and the agent `docker run` uses `--runtime runsc`, `--workdir=/`, and `--security-opt=no-new-privileges`.
- Vultr and OVH hosts created from FCT run their agent containers under gVisor: provisioning installs `runsc` (cloud-init for Vultr, bootstrap path for OVH) and the agent `docker run` adds `--runtime runsc`, `--workdir=/`, and `--security-opt=no-new-privileges`.
- `imbue_cloud` *fast path* (the common case) inherits runsc through the OVH-backed bake — the adopted pre-baked container was built under runsc. Already-baked pool hosts continue running as-is (no runsc) until their next bake — no migration.
- `imbue_cloud` *slow/rebuild path* is NOT covered by this change: it cannot be done via the `imbue_cloud` template (adding `start_arg` there breaks the fast path, which minds attempts first and which rejects `--start-arg`), and the slow path builds its delegated vps_docker provider with default config (no `docker_runtime`/run-args). Full coverage needs a code change in `mngr_imbue_cloud` (+ minds bootstrap) — deferred as a follow-up.
- Lima VMs boot a pinned Debian 12 genericcloud image instead of Ubuntu 24.04; provisioning installs the same toolchain via apt, so direct-in-VM Lima behavior is unchanged aside from the lighter base OS.
- Any Lima user can now inject arbitrary extra `docker run` args into the agent container via the new config field (empty by default, so no behavior change unless set).
- When `docker_runtime="runsc"` is set but `runsc` is not registered on the host/VM, container creation fails with Docker's native "unknown runtime" error (unchanged existing behavior). With `install_gvisor_runtime=true`, provisioning registers it first, so this should not occur on a clean bring-up.
- New host bring-up is slightly slower on first boot (one extra apt round-trip + Docker restart to install/register `runsc`); idempotent on re-runs and a no-op where `runsc` is already present.

## Changes

### `libs/mngr_lima` (code)

- Switch the default Lima VM image from Ubuntu 24.04 to a pinned Debian 12 "bookworm" genericcloud image for both architectures (the `DEFAULT_IMAGE_URL_AARCH64` / `DEFAULT_IMAGE_URL_X86_64` constants), using a specific dated snapshot URL rather than `latest/`.
- Add a general-purpose config field to the Lima provider for extra `docker run` arguments applied to the agent container (defaults to empty), and pass it into the inner container creation alongside the existing `--runtime` argument.
- Update the relevant field/constant docstrings to reflect the new default OS and the new passthrough field.
- Add a per-PR changelog entry under `libs/mngr_lima/changelog/`.

### `libs/mngr_vps_docker` (no code change expected)

- No code change: the provider already forwards template `start_arg` to the agent `docker run` and already supports `docker_runtime` + `install_gvisor_runtime`. Confirm during implementation; add a changelog entry only if a code change turns out to be needed.

### `forever-claude-template/.mngr/settings.toml` (config only, in the external worktree)

- `[providers.lima]`: enable `install_gvisor_runtime`, set `docker_runtime="runsc"`, and set the new extra-container-run-args field to `["--workdir=/", "--security-opt=no-new-privileges"]`.
- Add `[providers.vultr]` and `[providers.ovh]` blocks: enable `install_gvisor_runtime` and set `docker_runtime="runsc"`.
- `[create_templates.vultr]` and `[create_templates.ovh]`: add `start_arg__extend = ["--security-opt=no-new-privileges", "--workdir=/"]`; replace the existing "not enabled here yet" comments with the now-active rationale.
- `[create_templates.imbue_cloud]`: intentionally left unchanged — adding `start_arg` here would break the fast path (minds tries `fast_mode=require` first, which rejects `--start-arg`). The fast path gets runsc from the OVH bake instead.
- Leave the existing `[providers.docker]` block unchanged (already on runsc).

### Validation (required before "done")

- Manual one-off bring-up of a Lima host (docker-in-Lima, Debian + runsc) and a VPS host (OVH and/or Vultr, runsc) from FCT, confirming the agent container is actually running under `runsc` and reachable.
- Unit coverage asserting the new Lima extra-run-args field reaches the agent `docker run` invocation (alongside `--runtime`), plus the existing gVisor-install-block unit coverage.
- Rely on CI acceptance/release markers for full provider bring-up; do not block on running all of those locally.

### Out of scope

- Syncing the updated mngr into FCT's `vendor/mngr` (normal release-minds flow).
- Migrating or draining existing pre-baked `imbue_cloud` pool hosts.
- Alpine or any non-apt base image (would require rewriting the apt-based provisioning).
- The `imbue_cloud` slow/rebuild path under runsc (see Overview) — needs a `mngr_imbue_cloud` + minds-bootstrap code change to propagate `docker_runtime` + run-args into the delegated vps_docker provider; deferred as a follow-up. The fast path (the common case) is covered.
