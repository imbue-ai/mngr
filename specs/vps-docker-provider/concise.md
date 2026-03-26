# VPS Docker Provider

## Overview

* Two new packages: `mng_vps_docker` (base classes + shared infrastructure) and `mng_vultr` (Vultr-specific implementation)
* `mng_vps_docker` provides two base provider classes for running Docker containers on VPS instances, differing in what they treat as "the host":
  * **AlwaysOnVpsProvider** -- VPS is always running; the Docker container is the host. Stopping the host does `docker stop`. Idle timeout stops the container. Snapshots use `docker commit` on the remote Docker daemon. State volume uses a Docker named volume on the VPS (same pattern as the existing Docker provider's state container).
  * **EphemeralVpsProvider** -- VPS lifecycle is the host lifecycle. Starting/stopping the host starts/stops the VPS. Idle timeout stops the VPS. Snapshots use the VPS provider's native snapshot API. Data persists via provider-specific external storage (e.g., block storage volumes), exposed through abstract methods that child classes implement to provide a `Volume`.
* Both base classes share common infrastructure: VPS provisioning/teardown via cloud provider API, Docker installation via SSH, SSH key management, container setup with sshd, and ProxyJump-based SSH access to containers through the VPS
* 1:1 mapping: each VPS runs exactly one Docker container
* SSH host keys are generated locally and injected into the VPS at creation time via cloud-init, then added to local known_hosts -- no TOFU needed. When VPS IP changes (destroy/recreate), the old known_hosts entry is removed and replaced.
* Vultr implementation uses `vultr-python` SDK. VPS plan/region/etc. are configurable as build and/or start args (similar to how the Modal provider handles GPU/region/etc.).

## Expected Behavior

* `mng create --provider vultr-always-on` provisions a Vultr VPS, installs Docker via SSH, runs a container with sshd, and returns an online host accessible via `ssh -J user@vps root@localhost:2222`
* `mng create --provider vultr-ephemeral` does the same, but also attaches a Vultr Block Storage volume and mounts it as the Docker named volume's backing store
* `mng stop` on an always-on host does `docker stop` on the remote container; the VPS keeps running. `mng start` does `docker start`.
* `mng stop` on an ephemeral host halts the Vultr VPS instance (which preserves disk state). `mng start` starts the VPS and then starts the Docker container.
* `mng destroy` on either variant destroys the Docker container, the VPS instance, and any associated block storage volumes
* `mng ls` works for both online and offline hosts. For always-on hosts, offline state data is read via SSH to the VPS + `docker exec` into a state container (same pattern as existing Docker provider). For ephemeral hosts, offline discovery relies on locally cached certified data (since the VPS itself is down).
* `mng snapshot create` uses `docker commit` for always-on hosts and the Vultr snapshot API for ephemeral hosts
* `mng ssh` connects through the VPS as a jump host to the container
* Host volumes work identically to the existing Docker provider: a Docker named volume on the VPS is mounted into the container, `host_dir` is symlinked to a per-host subdirectory on that volume
* Idle timeout behavior: always-on stops the container; ephemeral stops the VPS. Both use the same activity watcher mechanism as the existing Docker provider.
* Build args and start args are passed through to control VPS provisioning (plan, region, OS image, etc.) similar to how the Modal provider handles resource configuration

## Changes

* New package `libs/mng_vps_docker/` containing:
  * Base config class extending `ProviderInstanceConfig` with common VPS Docker settings (default idle timeout, idle mode, activity sources, default image, SSH connect timeout, cloud-init script)
  * Abstract VPS client interface that concrete providers implement (create_instance, destroy_instance, start_instance, stop_instance, get_instance_status, get_instance_ip, attach_volume, detach_volume, create_snapshot, list_snapshots, delete_snapshot, etc.)
  * `AlwaysOnVpsProvider(BaseProviderInstance)` -- manages VPS lifecycle + Docker containers. Treats container as host. Uses Docker named volume + state container on the remote VPS for persistence. Snapshots via `docker commit` over SSH.
  * `EphemeralVpsProvider(BaseProviderInstance)` -- manages VPS lifecycle as host lifecycle. Treats VPS as host. Child classes implement abstract methods for external storage (Volume). Snapshots via VPS provider API.
  * Shared utilities: SSH key generation/injection via cloud-init, Docker installation script, container setup (sshd, ProxyJump config), known_hosts management (add on create, remove on destroy/recreate)
  * Remote Docker helpers: running Docker commands over SSH (since we don't use the Docker SDK -- the Docker daemon is on the VPS, not local)
* New package `libs/mng_vultr/` containing:
  * Vultr-specific config extending the base VPS Docker config (API key, default plan, default region, default OS)
  * Concrete VPS client implementation using `vultr-python` SDK
  * Two backend registrations: one for `AlwaysOnVpsProvider` ("vultr-always-on") and one for `EphemeralVpsProvider` ("vultr-ephemeral")
  * Vultr Block Storage volume implementation for the ephemeral variant
* Both packages register via pluggy entry points (same pattern as `mng_modal`)
* Reuse existing shared infrastructure from `imbue.mng.providers`: `ssh_host_setup` (package installation, SSH config, activity watcher), `ssh_utils` (keypair management, pyinfra host creation, known_hosts, wait_for_sshd), `base_provider.py`
* Docker commands on the remote VPS are executed via SSH (using pyinfra or subprocess over the VPS SSH connection), not via the Docker SDK's remote host feature -- the VPS is accessed purely through SSH
