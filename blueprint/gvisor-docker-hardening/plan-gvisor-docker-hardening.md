# Plan: harden docker invocations with gVisor / runsc

## Overview

- Run untrusted agents under the gVisor (`runsc`) container runtime instead of plain `runc`, to shrink the kernel attack surface for the security story around untrusted agents.
- Introduce a typed, per-provider `docker_runtime` config var (default `None`) on the three docker-based providers (`docker`, `mngr_vps_docker`, `mngr_lima`); when set, the provider passes `--runtime=<value>` to `docker run`. This is a small, deliberate change to provider code (an accepted divergence from "don't touch the provider").
- Start by *enabling* gVisor on the **normal docker provider only** (via the forever-claude-template config), proving the end-to-end path before extending to vps/lima.
- Make gVisor overridable so CI/Modal (which can't run gVisor) can force `runc` via the existing `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc` env layer.
- Tighten related hardening: drop `--cap-add=SYS_PTRACE`, add `--security-opt=no-new-privileges`, document the Chromium `--no-sandbox` requirement, and reduce the `app_watcher` inotify-fallback poll latency.

## Expected behavior

- A new agent created from the FCT docker config runs in a container started with `--runtime=runsc`, `--security-opt=no-new-privileges`, and **without** `--cap-add=SYS_PTRACE`.
- When `docker_runtime` is unset/`None` (the default for every provider out of the box), behavior is identical to today — no `--runtime` flag is added — so existing users and CI see no change unless they opt in.
- Setting `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc` overrides the FCT setting and forces plain `runc`, letting the docker-based tests keep running in GitHub CI and on Modal.
- If `runsc` is selected but not installed/registered on the host, container creation fails with Docker's native "unknown runtime" error (no silent fallback to an unsandboxed runtime). For the local docker provider this is treated as a documented-by-error prerequisite (the minds docker provider is development-only).
- On vps and lima hosts, when the new `install_gvisor_runtime` flag is enabled, provisioning installs and registers `runsc` idempotently (no-op if it is already present in the base image); these hosts still *run* on `runc` in this change (install ahead of a later enable).
- Chromium/Playwright continues to work: because the container runs as root, Chromium already requires `--no-sandbox`; this is now documented in the FCT `CLAUDE.md` so agent/consumer usage applies it. Under gVisor, gVisor itself is the sandbox boundary, so dropping the in-browser sandbox is acceptable.
- File watching keeps working under gVisor: `app_watcher` still prefers inotify but its mtime-polling fallback latency drops from 10s to 5s, bounding worst-case detection latency for the vps/lima cases where host-side writes don't raise in-sandbox inotify events. `session_watcher` (already 1s) and the backup services (intentional timer-based pollers, no inotify) are unaffected.

## Changes

### mngr monorepo (this repo; vendored into FCT)

- Add a `docker_runtime: str | None` field (default `None`, accepts any non-empty string, passed through to `docker run --runtime`) to the docker provider config, and wire it into the docker run command so `--runtime=<value>` is emitted only when set.
- Add the same `docker_runtime` knob to the `mngr_vps_docker` and `mngr_lima` provider configs, wired into each provider's docker-run path, so the override exists everywhere even though only the normal docker provider is enabled now.
- Add an `install_gvisor_runtime` boolean flag (default `False`) to the `mngr_vps_docker` and `mngr_lima` provider configs.
- Add idempotent host-provisioning for `runsc` to `mngr_vps_docker` and `mngr_lima`, gated by `install_gvisor_runtime`: install via gVisor's official APT repository (pinned version), register with `runsc install`, restart Docker — all at provision time (before any agent containers), and a no-op when `runsc` is already present.
- Add unit tests for the config-var plumbing and for the rendered docker-run command (asserts `--runtime=runsc` present when set, absent when `None`); add tests for the install flag gating. Add per-project changelog entries for each touched project.

### forever-claude-template repo

- In `.mngr/settings.toml` `[create_templates.docker]` (and/or `[providers.docker]`): set `docker_runtime = "runsc"`, remove `--cap-add=SYS_PTRACE` from `start_arg`, and add `--security-opt=no-new-privileges` to `start_arg`.
- In the lima and vps create templates: set `install_gvisor_runtime = true` so `runsc` is installed ahead of the later enable (while those providers keep `docker_runtime` unset → `runc`).
- Document in `CLAUDE.md` that Chromium/Playwright must be launched with `--no-sandbox` (gVisor / root-in-container is the sandbox boundary); leave `scripts/deferred_install.sh` unchanged.
- Reduce `app_watcher`'s poll-interval constant from 10s to 5s; leave `session_watcher` and the backup services untouched.
- Sync `vendor/mngr` to pick up the provider changes (the standard vendor/mngr sync step).
- Add per-project changelog entries as required.

### Explicitly out of scope (this change)

- Enabling gVisor on vps/lima at runtime (they install but keep running on `runc`).
- A Chromium wrapper/shim or executable-path env var (documentation only).
- Additional hardening flags beyond `no-new-privileges` (cap-drop, resource limits, read-only rootfs).
- A custom preflight check for missing `runsc` (rely on Docker's native error).
- Any backup-interval changes (backups are intentional timer pollers, unaffected by gVisor).
