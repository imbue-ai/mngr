# Docker-in-Docker on Modal: Release Test Verification

**WARNING: Builds take ~1 hour and frequently OOM. Only kill after exactly 70 minutes.**

## Goal

Get the vanilla release flow working end-to-end:
1. `Dockerfile.release` builds successfully on Modal with `enable_docker=true`
2. `just test-offload-release` runs the release tests via Offload on Modal
3. `test_multiple_snapshots_ordering` passes

## Current status

The Dockerfile.release is too large for Modal's image builder. The builder worker OOMs
during the post-build save/materialize phase — regardless of whether `enable_docker` is
set. This is a Modal builder memory limit, not an `enable_docker` issue.

The builder sometimes succeeds (#14: ~56 min, save took 6.55s) and sometimes OOMs
(#15, #16: "Worker disappeared"), depending on Modal-side memory pressure.

## Root cause: `uv sync --all-packages`

The Dockerfile does `uv sync --all-packages` which installs **190 packages** (the
entire monorepo). The release tests only need a small subset. This bloats the image
and pushes the builder over its memory limit during the save phase.

## Experiment log

| # | What changed | enable_docker | Time | Outcome | Lesson |
|---|---|---|---|---|---|
| 1 | Docker CE via apt in Dockerfile | yes | ~60 min | Worker crashed (OOM) | Docker CE (~1.5 GB) is too large — use static binaries |
| 2 | Pre-built base pushed to GHCR | yes | <1 min | Auth failure | GHCR packages default private |
| 3 | Pre-built base on Docker Hub (Docker CE) | yes | ~60 min | Worker crashed (OOM) | Pre-built base doesn't help if image too large |
| 4 | Slim: python:3.11-slim + git/curl/uv, no Docker, no code | yes | **910ms** | **Saved OK** | Small images save fast |
| 5–10 | Various combos of static Docker, build-essential, .dockerignore | yes | 10–110 min | Killed prematurely | Killed too early |
| 11 | Vanilla Dockerfile.release | yes | >10 min | Killed prematurely | Killed too early |
| 12 | Minimal: OS + Docker + runc, no code/deps | yes | **~2 min** | **Base saved OK** | Small images save fast; Docker binaries not the problem |
| 13 | Full Dockerfile.release, 10 min timeout | yes | >10 min | Killed prematurely | Killed too early |
| 14 | Full Dockerfile.release, CMD exits immediately | yes | **~56 min** | **Image saved** (6.55s save, 3375s build). sandbox_init_cmd failed: `git apply` error | Build CAN complete. ~56 min. |
| 15 | Vanilla Dockerfile.release, clean patch, 70 min timeout | yes | ~50 min | **Worker disappeared (OOM)** | OOM during post-build save phase |
| 16 | Vanilla Dockerfile.release, `enable_docker` OFF | **no** | ~70 min | **Worker disappeared (OOM)** | **Same OOM without `enable_docker`!** `enable_docker` is NOT the cause |

## Bisect experiment: isolating which Dockerfile.release delta causes the hang

Used `modal.Image.from_dockerfile(..., force_build=True)` via `build_sandbox_image.py`
with app name `danver-modal-release-proving`. Timeout = 240s (3X the sandbox baseline).
Starting from the full sandbox Dockerfile content in the Dockerfile.release file, then
cumulatively re-enabling each release-specific change.

| # | Cumulative changes enabled | Time | Result |
|---|---|---|---|
| B0 | None (functionally identical to sandbox Dockerfile) | 109.3s | OK |
| B1 | + `iproute2`, `iptables` in apt-get | 133.0s | OK |
| B2 | + Docker static binaries RUN | 118.1s | OK |
| B3 | + runc binary RUN | 106.1s | OK |
| B4 | + iptables-legacy switch RUN | 103.1s | OK |
| B5 | + COPY dockerd scripts + chmod + `ENV BASH_ENV` | 240s | **KILLED (timeout)** |
| B5a | + COPY dockerd scripts + chmod only (no `ENV BASH_ENV`) | 90.1s | OK |
| B5b | + COPY dockerd scripts + chmod + `ENV BASH_ENV` (re-run) | 86.4s | **Worker disappeared (OOM)** |

## Root cause: `ENV BASH_ENV=/ensure-dockerd.sh`

The single line `ENV BASH_ENV=/ensure-dockerd.sh` causes the build to hang or OOM.

`BASH_ENV` is sourced before **every** `bash -c` command, including during the Docker
image build. The script (`ensure-dockerd.sh`) runs:

```bash
if [ -x /start-dockerd.sh ] && ! docker info >/dev/null 2>&1; then
    /start-dockerd.sh >/dev/null 2>&1 || true
fi
```

During the image build on Modal's builder (which uses gVisor), every RUN instruction
that invokes bash will attempt to run `docker info` and then `/start-dockerd.sh`.
This either hangs (gVisor can't run dockerd) or causes OOM from repeated failed
daemon startup attempts accumulating resources.

The `uv sync --all-packages` step spawns many subprocesses, each of which triggers
this BASH_ENV script, compounding the problem.

## Key findings

1. **`enable_docker` is NOT the cause.** Exp #16 OOMs identically without it.
2. **`ENV BASH_ENV=/ensure-dockerd.sh` IS the cause.** It runs dockerd startup
   logic during every bash-invoked RUN instruction in the image build.
3. **All other release-specific changes are fine.** Docker binaries, runc, iptables,
   iproute2 all build within ~90-133s (comparable to the sandbox baseline of ~109s).
4. **Small images work fine.** Exps #4 and #12 save in <2 min.

## Offload layer-by-layer experiments

After upgrading offload to build Dockerfiles layer by layer (each instruction as a
separate `dockerfile_commands()` call), the `BASH_ENV` problem manifests differently:
ENV persists across layers, so `sandbox_init_cmd` (which runs as a final build step)
still sources `ensure-dockerd.sh`.

| # | What changed | Time | Result |
|---|---|---|---|
| B8 | Vanilla Dockerfile.release (BASH_ENV before RUN), offload layers | ~10 min | **Hung** -- `sandbox_init_cmd` triggers BASH_ENV -> dockerd |
| B9 | BASH_ENV removed, `/start-dockerd.sh` in sandbox_init_cmd | ~3 min | **Failed** -- iptables nat table not available during build |
| B10 | BASH_ENV after last RUN + guarded ensure-dockerd.sh | ~3 min | **PASSED** -- test_multiple_snapshots_ordering passed in 45s |

## Resolution

Three changes fixed the issue:

1. **Moved `ENV BASH_ENV=/ensure-dockerd.sh` after the last RUN instruction** in
   Dockerfile.release, so it doesn't affect image build layers.

2. **Guarded `ensure-dockerd.sh`** with an iptables nat table check so it silently
   skips when kernel capabilities aren't available (during image build):
   ```bash
   if [ -x /start-dockerd.sh ] && ! docker info >/dev/null 2>&1; then
       iptables-legacy -t nat -L >/dev/null 2>&1 && /start-dockerd.sh >/dev/null 2>&1 || true
   fi
   ```

3. **Made Dockerfile.release a superset of the sandbox Dockerfile** (same base layers),
   so offload's layer caching can reuse the sandbox image layers. This reduces
   build time from ~56 minutes to ~3 minutes.

## Key findings

1. **`enable_docker` is NOT the cause.** Exp #16 OOMs identically without it.
2. **`ENV BASH_ENV=/ensure-dockerd.sh` before RUN instructions IS the cause** of the
   hang/OOM. It runs dockerd startup logic during every bash-invoked build step.
3. **All other release-specific changes are fine.** Docker binaries, runc, iptables,
   iproute2 all build within ~90-133s.
4. **The ~56 min build time** in earlier experiments was caused by `BASH_ENV` triggering
   dockerd startup attempts during `uv sync`, not by `uv sync` itself being slow.
