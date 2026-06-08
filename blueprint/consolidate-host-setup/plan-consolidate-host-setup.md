# Consolidate host setup scripts (single source of truth, re-runnable on the imbue_cloud slow path)

## Refined prompt

we want to consolidate the host setup scripts so that we can re-run the initial setup host level scripts for minds workspaces created via imbue_cloud when we are running via the slow method of creating the host (in order to make sure everything is set up consistently)

In the "slow" path for creating minds workspaces via imbue_cloud in the minds app (ie, when imbue_cloud is selected as the compute provider), it does NOT currently do a *full* provisioning *of the host*, ie, only some of the host setup is done (even if all of the agent setup is done)

This matters because we want the slow path to execute correctly, even against a host that was baked with a really old version...

obviously we do NOT want to re-create the OS, but basically everything after that should be idempotent (minus the SSH host key addition, which does need to be handled correctly / not broken or reset)

Let's do this the right way with the single source of truth, and make sure that the slow path applies all of those idempotent setup commands so that, if they *do* change, we're more likely to be able to get away with re-using an old host

Part of the motivation is also to fix the issue outlined in the (now-deleted) `specs/imbue-cloud-slow-path-runsc.md`: the imbue_cloud slow/rebuild path does not run the agent container under gVisor (runsc).

As part of this, we ought to specify the exact version of docker that we're installing as well.

* The single source of truth is one structured, parameterized, ordered list of idempotent host-setup steps; cloud-init wraps that list into `runcmd` for first boot, and a new `apply_host_setup_on_outer()` runs the same list over SSH for re-provisioning.
* The re-runnable host setup includes: pinned Docker install, gVisor `runsc` install (gated by `install_gvisor_runtime`), sshd `MaxSessions`/`MaxStartups` tuning, base packages (`rsync`, `inotify-tools`, `jq`, `curl`, `ca-certificates`, `gnupg`), and qemu purge — each config-gated.
* It explicitly EXCLUDES SSH host-key injection / `ssh_deletekeys` (first-boot-only) so re-running never resets the VPS root host key or breaks `known_hosts`.
* Docker is pinned to a specific version (`29.5.1` on Debian 12 / bookworm) and enforced via the docker apt repo plus `apt-get install -y --allow-downgrades docker-ce=<pin> docker-ce-cli=<pin> containerd.io docker-buildx-plugin docker-compose-plugin` (upgrade/downgrade authoritative); `runsc` is pinned to a specific dated gVisor release; exact apt/version strings are confirmed against the repos at implementation time.
* Confirmed gap: OVH never installs `runsc` today (no cloud-init; the "OVH bootstrap path" the runsc-everywhere plan intended was never implemented), and the imbue_cloud pool is OVH-backed — so this consolidation is also what makes OVH's `install_gvisor_runtime`/`docker_runtime` config effective.
* OVH `_provision_vps` calls `apply_host_setup_on_outer()` over SSH with `install_gvisor_runtime=true`; its existing `install_required_outer_packages` / `purge_qemu_packages` are deleted and folded into the shared list as config-gated steps; sshd tuning is gained on OVH too.
* The imbue_cloud slow path (`_rebuild_leased_container`) calls `apply_host_setup_on_outer()` with `install_gvisor_runtime=true` so old/pre-runsc leased hosts self-heal; it runs after container teardown and before rebuild, and any failure is fatal.
* `apply_host_setup_on_outer()` is NOT folded into `create_host_on_existing_vps`, so the fresh-create/bake path on cloud-init backends does not double-run cloud-init's work.
* `ImbueCloudProviderConfig` extends `VpsDockerProviderConfig` (inheriting `docker_runtime` / `install_gvisor_runtime` / `default_start_args`); minds bootstrap writes `docker_runtime="runsc"`, `install_gvisor_runtime=true`, and `default_start_args=["--workdir=/", "--security-opt=no-new-privileges"]` into `[providers.imbue_cloud_<slug>]`; `_build_delegated_vps_provider` forwards them onto the delegated `VpsDockerProviderConfig`.
* The pool must be re-baked so fast-path hosts are correct (the fast path does no host setup); the slow path self-heals everything else.
* Lima is out of scope (its duplicate runsc block in `lima_yaml.py` is noted as future cleanup); the stale `specs/imbue-cloud-slow-path-runsc.md` is deleted (superseded).
* Verification is unit-level in this change (forwarding + the shared step list); the end-to-end runsc check (`docker inspect --format '{{.HostConfig.Runtime}}'` → `runsc`) is tracked separately.

## Overview

- Today host-level provisioning (Docker install, gVisor install, sshd tuning, packages) lives only inside the cloud-init YAML string in `mngr_vps_docker/cloud_init.py`, which runs once at VPS first boot and cannot be re-applied over SSH. The imbue_cloud slow path rebuilds only the container layer (`create_host_on_existing_vps`), so a leased pool host keeps whatever host setup it was baked with — forever.
- This change extracts the host-setup steps into one structured, idempotent, config-gated step list (the single source of truth). cloud-init wraps it for first boot; a new SSH runner re-applies the same list against an already-running host. The SSH-host-key injection stays first-boot-only and is deliberately excluded so re-runs never reset the VPS root host key or break `known_hosts`.
- The imbue_cloud slow path now re-applies the full host setup on the leased host (after container teardown, before rebuild), so old hosts get brought up to current: pinned Docker, runsc installed, sshd tuned. This makes reusing an old baked host far more likely to succeed when setup commands change.
- It fixes the deferred runsc gap as part of the same work: the slow-path rebuild gets `docker_runtime=runsc` plus the `--workdir=/` / `--security-opt=no-new-privileges` hardening args, threaded through config rather than the create template (which the fast path would reject). Investigation also revealed OVH never installs runsc at all, so the shared SSH path is wired into the OVH bake too — fixing both paths.
- Docker is pinned to an exact version and enforced (upgrade/downgrade) via the apt repo, so bakes and re-provisions are reproducible instead of "whatever `get.docker.com` served that day".

## Expected behavior

- Creating an imbue_cloud workspace via the slow path (`fast_mode=prevent`) re-applies all idempotent host setup on the leased host before rebuilding the container: it ensures the pinned Docker version, installs/registers `runsc` if missing, tunes sshd, and ensures base packages — then rebuilds the agent container under `runsc` with the hardening args.
- A workspace created via the slow path against an old/pre-runsc baked host now ends up running its agent container under gVisor (`docker inspect --format '{{.HostConfig.Runtime}}'` reports `runsc`), instead of `runc`.
- The fast path (`fast_mode=require`) is unchanged: it still adopts a pre-baked container as-is and still rejects `--image`/`--start-arg`. Fast-path correctness depends on the pool being (re-)baked with the new setup.
- OVH-baked hosts now actually get `runsc` installed and sshd tuned during provisioning (previously `[providers.ovh] install_gvisor_runtime=true` was a silent no-op), so the pool bake produces runsc-capable hosts.
- The SSH host key behavior is preserved: re-provisioning never runs `ssh_deletekeys` or re-injects the VPS host key; the VPS root key and existing `known_hosts` entries are untouched. The rebuilt container's SSH host key continues to be re-scanned and recorded as it is today.
- Docker version is deterministic: provisioning installs exactly the pinned version, upgrading or downgrading the host's Docker as needed; a host that already has the pinned version is a no-op.
- The slow path fails loudly (aborts the create) if host setup cannot be applied, rather than silently rebuilding on a misconfigured host. The fresh-create path on cloud-init backends (Vultr) is unaffected and does not double-run setup.
- No behavior change for Lima or the local docker provider.

## Changes

- Add a new single-source-of-truth module in `mngr_vps_docker` that defines the ordered, idempotent, parameterized host-setup steps (pinned Docker install, config-gated `runsc` install, sshd `MaxSessions`/`MaxStartups` tuning, base-package install, config-gated qemu purge) and an SSH runner (`apply_host_setup_on_outer()`) that executes them against an existing outer host.
- Refactor `cloud_init.py` so its `runcmd` is generated from the same shared step list (no duplicated command text), keeping the first-boot-only pieces (SSH host-key injection, `ssh_deletekeys`, `ssh_pwauth`, the `mngr-ready` marker) in the cloud-init wrapper and out of the re-runnable list.
- Pin the Docker version (and the gVisor `runsc` release) as shared constants, and change the Docker install step to use the docker apt repo with an explicit, allow-downgrades pinned install of `docker-ce`/`docker-ce-cli`/`containerd.io`/`docker-buildx-plugin`/`docker-compose-plugin`.
- Wire OVH's `_provision_vps` to call `apply_host_setup_on_outer()` (with `install_gvisor_runtime=true`) over SSH, and delete `install_required_outer_packages` / `purge_qemu_packages` from `mngr_ovh/bootstrap.py`, folding their behavior into the shared step list as config-gated steps; OVH gains sshd tuning via the same path.
- Have the imbue_cloud slow path (`_rebuild_leased_container`) call `apply_host_setup_on_outer()` (with `install_gvisor_runtime=true`) on the leased outer, after `teardown_container_on_existing_vps` and before `create_host_on_existing_vps`, treating any failure as fatal.
- Make `ImbueCloudProviderConfig` extend `VpsDockerProviderConfig` so it carries `docker_runtime` / `install_gvisor_runtime` / `default_start_args`, and have `_build_delegated_vps_provider` forward those onto the delegated `VpsDockerProviderConfig` (replacing today's defaults-only construction).
- Update minds bootstrap (`set_imbue_cloud_provider_for_account`) to write `docker_runtime="runsc"`, `install_gvisor_runtime=true`, and `default_start_args=["--workdir=/", "--security-opt=no-new-privileges"]` into the per-account `[providers.imbue_cloud_<slug>]` block.
- Add unit coverage that `_build_delegated_vps_provider` forwards the runtime fields and that the shared step list produces the expected idempotent commands for both the cloud-init wrapper and the SSH runner; leave end-to-end runsc verification to a separately-tracked manual/release check.
- Delete the superseded `specs/imbue-cloud-slow-path-runsc.md` (already removed); update any docstrings/comments referencing the old cloud-init-only host-setup or OVH bootstrap functions.
- Add changelog entries for the touched projects (`libs/mngr_vps_docker`, `libs/mngr_ovh`, `libs/mngr_imbue_cloud`, `apps/minds`); note in docs/comments that the pool must be re-baked to make fast-path hosts runsc-capable.
