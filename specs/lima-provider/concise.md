# Lima Provider

## Overview

* Add a "lima" provider to mngr that runs agents inside Lima VMs, giving users an easy way to run isolated VMs on their own machines without requiring Docker
* Lima wraps QEMU (Linux/KVM) and Apple Virtualization.framework (macOS/VZ), handling SSH key management, port forwarding, and VM lifecycle automatically
* Implemented as a separate plugin package (`libs/mngr_lima/`), following the same pattern as the Modal provider (`libs/mngr_modal/`)
* Includes a pre-built qcow2 default image (Ubuntu LTS with tmux, git, jq, openssh-server pre-installed), built with Packer and published to GitHub Releases for both aarch64 and x86_64
* Snapshots are deferred -- Lima only supports them on the QEMU backend (not VZ, the macOS default), and the feature is marked experimental

## Expected Behavior

### Creating a host

* `mngr create @.lima` creates a Lima VM with an auto-generated name (like `@.modal`)
* `mngr create my-agent@my-host.lima` creates an agent on a named Lima host
* The provider checks that `limactl` is installed and meets the minimum version requirement; fails with a helpful error if not
* A Lima instance named `mngr-{host_name}` is created using the default or user-supplied Lima YAML config
* The default image (pre-built qcow2 from GitHub Releases) is downloaded on first use; Lima's YAML config selects the correct architecture (aarch64 or x86_64)
* Lima picks the best VM backend automatically: VZ on macOS (faster, Apple-native), QEMU on Linux (KVM-accelerated)
* Default resources: 4 CPU, 4 GiB RAM, 100 GiB disk (Lima's defaults)
* A persistent volume directory is created at `~/.mngr/providers/lima/volumes/<host_id>/` and mounted into the VM at the mngr host_dir path (e.g. `/mngr`) via Lima's YAML mounts config
* The provider waits for cloud-init to complete before returning (the host is fully ready when `create_host` returns)
* Required packages (tmux, git, jq, sshd) are provisioned during `create_host` if not already present in the image
* The activity watcher is installed during provisioning (not baked into the image)
* A `shutdown.sh` script calling `sudo poweroff` is installed inside the VM as a fallback for idle shutdown
* SSH connection info is obtained by parsing `limactl show-ssh` output
* Any provided `authorized_keys` are appended to the VM's `~/.ssh/authorized_keys`
* The VM uses Lima's default user (matches host username, has passwordless sudo)
* No port forwarding is configured -- all access goes through SSH, consistent with mngr's model
* Host metadata (host_id, certified data, resources) is persisted in the volume directory so it's accessible when the VM is stopped

### Customizing the VM

* `--build-args` accepts a path to a Lima YAML config file for full customization (equivalent to Docker's `--build-args "--file path/to/Dockerfile"`)
* If no Lima YAML is provided, a sensible default is generated with the pre-built image, default resources, and volume mount
* `--start-args` passes extra flags directly to `limactl start` (the valid flags that `limactl start` actually accepts)
* Directory mounts can be configured via the Lima YAML config (through `--build-args` or a custom YAML file)

### Host lifecycle

* `stop_host`: calls `limactl stop` (graceful VM shutdown, preserves disk state)
* `start_host`: calls `limactl start`, then waits for SSH to come back up before returning
* `destroy_host`: calls `limactl delete --force` (removes VM and disk) and removes the host record
* `delete_host`: cleans up any remaining local state (volume directory, host record files in `~/.mngr/providers/lima/`)
* Default idle timeout: 800 seconds (same as Docker)
* On idle timeout, `limactl stop` is triggered from the host side; the in-VM `shutdown.sh` (`sudo poweroff`) serves as a fallback
* `rename_host` raises an error (Lima instances cannot be renamed)

### Host discovery

* `discover_hosts` filters `limactl list --json` output for instances whose name starts with `mngr-`
* Host state is mapped from Lima's instance status to mngr's `HostState` enum (Running -> RUNNING, Stopped -> STOPPED, etc.)
* If a non-mngr Lima instance happens to have the `mngr-` prefix, mngr will pick it up (same convention-based approach as Docker)
* When the VM is stopped, `get_host` returns an `OfflineHost` with metadata loaded from the persistent host record in the volume directory
* `get_host_resources` reads the configured resources stored in the persistent host record (not queried live from the VM)

### Concurrency

* File-based locking in the host volume directory (`~/.mngr/providers/lima/volumes/<host_id>/.lock`) prevents concurrent operations on the same Lima instance

### Plugin registration

* The plugin registers automatically when installed (via `[project.entry-points.mngr]` in pyproject.toml), like the Modal provider
* No explicit configuration needed -- installing the `imbue-mngr-lima` package enables the provider

### Error handling

* `limactl` stderr is parsed and mapped to appropriate mngr exceptions (`HostCreationError`, `HostNotFoundError`, etc.)
* Missing `limactl` binary triggers a clear error message explaining how to install Lima
* Version check at startup fails with a helpful error if the installed Lima version is too old

## Changes

### New package: `libs/mngr_lima/`

* `pyproject.toml` -- package metadata, dependencies (imbue-mngr, pyyaml), entry point registration (`[project.entry-points.mngr] lima = "imbue.mngr_lima.backend"`)
* `imbue/mngr_lima/__init__.py` -- `hookimpl = pluggy.HookimplMarker("mngr")`
* `imbue/mngr_lima/backend.py` -- `LimaProviderBackend` (implements `ProviderBackendInterface`), `@hookimpl register_provider_backend()`, version checking
* `imbue/mngr_lima/instance.py` -- `LimaProviderInstance` (extends `BaseProviderInstance`, implements `ProviderInstanceInterface`): create_host, stop_host, start_host, destroy_host, delete_host, discover_hosts, get_host, get_host_resources, etc.
* `imbue/mngr_lima/config.py` -- `LimaProviderConfig` (extends `ProviderInstanceConfig`): backend name, host_dir, default_image URL, default_idle_timeout, default_idle_mode, minimum_lima_version, etc.
* `imbue/mngr_lima/host_store.py` -- `LimaHostStore` and `HostRecord` for persisting host metadata (host_id, SSH info, certified data, resources) as JSON in the volume directory
* `imbue/mngr_lima/volume.py` -- `LimaVolume` wrapper for the host-side volume directory at `~/.mngr/providers/lima/volumes/<host_id>/`
* `imbue/mngr_lima/errors.py` -- Lima-specific error types (LimaNotInstalledError, LimaVersionError, LimaCommandError)
* `imbue/mngr_lima/lima_yaml.py` -- Generate default Lima YAML configs, merge user-supplied YAML overrides
* `imbue/mngr_lima/constants.py` -- Lima instance name prefix, default image URLs, minimum version, etc.
* `imbue/mngr_lima/testing.py` -- Test utilities and fixtures
* `imbue/mngr_lima/conftest.py` -- Shared pytest fixtures

### New scripts: `scripts/`

* `scripts/build-lima-image.sh` -- Packer build script for the default qcow2 image (Ubuntu LTS + mngr dependencies); builds both aarch64 and x86_64 variants
* `scripts/packer/` -- Packer template (HCL) and provisioning scripts for the Lima image
* `scripts/publish-lima-image.sh` -- Uploads built images to GitHub Releases via `gh release`

### Existing files to modify

* `pyproject.toml` (root) -- add `imbue-mngr-lima` to workspace members
* `libs/mngr/pyproject.toml` -- no changes needed (Lima is a separate plugin, not a built-in provider)

### Capabilities summary

| Capability | Supported |
|---|---|
| `supports_snapshots` | `False` (deferred) |
| `supports_shutdown_hosts` | `True` (via `limactl stop/start`) |
| `supports_volumes` | `True` (host-side directory mounted into VM) |
| `supports_mutable_tags` | `True` (stored in local JSON, like local provider) |
