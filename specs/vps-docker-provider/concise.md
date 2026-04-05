# VPS Docker Provider

## Overview

* Two new packages: `mngr_vps_docker` (base classes + shared infrastructure) and `mngr_vultr` (Vultr-specific implementation)
* `mngr_vps_docker` provides a single provider class (`VpsDockerProvider`) for running Docker containers on VPS instances
* The VPS stays running at all times; the Docker container is the mngr "host". Stopping the host stops the container; the VPS keeps running. Destroying the host destroys both the container and the VPS.
* 1:1 mapping: each VPS runs exactly one Docker container. Docker is used purely for consistent provisioning, not for multiplexing.
* Shared infrastructure: VPS provisioning via cloud provider API, Docker installation via SSH, SSH key management, container setup with sshd, direct SSH access to containers via the VPS's public IP on a dedicated port
* SSH host keys are generated locally and injected into the VPS at creation time via cloud-init, then added to local known_hosts -- no TOFU needed. When a VPS is destroyed, the known_hosts entry is removed.
* All host records and agent data are stored on a Docker state volume on the VPS itself (same pattern as the existing Docker provider's state container). State is self-contained with the infrastructure.
* Vultr implementation uses raw HTTP calls to the Vultr API v2 (no third-party SDK). VPS plan/region/etc. are configurable as build args (similar to how the Modal provider handles GPU/region/etc.).

## Expected Behavior

* `mngr create --provider vultr` provisions a Vultr VPS, installs Docker via SSH, runs a container with sshd, and returns an online host accessible via `ssh -p 2222 root@<vps-ip>`
* `mngr stop` does `docker stop` on the remote container; the VPS keeps running. `mngr start` does `docker start`.
* `mngr destroy` destroys the Docker container, the host volume, and the VPS instance itself
* `mngr ls` works for both online and offline containers. Offline state data is read via SSH to the VPS + `docker exec` into a state container (same pattern as existing Docker provider).
* `mngr snapshot create` uses `docker commit` on the remote container
* `mngr ssh` connects directly to the container via the VPS's public IP on port 2222
* Host volumes work identically to the existing Docker provider: a Docker named volume on the VPS is mounted into the container, `host_dir` is symlinked to a per-host subdirectory on that volume
* Idle timeout stops the container (not the VPS). Uses the same activity watcher mechanism as the existing Docker provider.
* Build args control VPS provisioning (plan, region, OS image) and Docker image building (Dockerfile, base image). Start args are passed through to `docker run`.

## Changes

* New package `libs/mngr_vps_docker/` containing:
  * Base config class extending `ProviderInstanceConfig` with common VPS Docker settings (default idle timeout, idle mode, activity sources, default image, SSH connect timeout, cloud-init support)
  * Abstract VPS client interface that concrete providers implement (create_instance, destroy_instance, get_instance_status, get_instance_ip, wait_for_instance_active, create_snapshot, list_snapshots, delete_snapshot, upload_ssh_key, delete_ssh_key)
  * `VpsDockerProvider(BaseProviderInstance)` -- manages VPS lifecycle + Docker containers. Treats container as host. Uses Docker state volume + state container on the remote VPS for host record persistence. Snapshots via `docker commit` over SSH.
  * Shared utilities: SSH key generation/injection via cloud-init, Docker installation script, container setup (sshd, direct SSH config), known_hosts management (add on create, remove on destroy)
  * Remote Docker helpers: running Docker commands over SSH (since the Docker daemon is on the VPS, not local)
* New package `libs/mngr_vultr/` containing:
  * Vultr-specific config extending the base VPS Docker config (API key, default plan, default region, default OS)
  * Concrete VPS client implementation using raw HTTP calls to Vultr API v2
  * Backend registration: "vultr" provider backend
* `mngr_vultr` registers via pluggy entry points (same pattern as `mngr_modal`)
* Reuse existing shared infrastructure from `imbue.mngr.providers`: `ssh_host_setup` (package installation, SSH config, activity watcher), `ssh_utils` (keypair management, pyinfra host creation, known_hosts, wait_for_sshd), `base_provider.py`
* Docker commands on the remote VPS are executed via SSH (using subprocess over the VPS SSH connection), not via the Docker SDK's remote host feature -- the VPS is accessed purely through SSH
