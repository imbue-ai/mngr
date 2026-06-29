# Unabridged Changelog - mngr_lima

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_lima/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-22

Add `flock` (the `util-linux` package) to the Lima VM provisioning script's required-package check.

`flock` now backs mngr's unified cross-actor host lock and the in-host idle-shutdown watcher, so it must be present on Lima hosts. It is already present on the standard Debian images Lima uses, so this only installs it on minimal/custom images that lack it.

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-16

Removed a redundant `register_marker("lima: ...")` call from the test conftest: the `lima` pytest marker is already auto-registered for the whole session by the resource_guards infrastructure (mngr_lima's `lima` resource guard), so the manual registration was duplicative. Test selection and the marker's meaning are unchanged.

`destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign (no failure); a resource that exists but could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a bookkeeping/record write failure), so `mngr destroy`/`cleanup` can surface it and exit with an informative, cause-specific code. See `specs/cleanup-error-aggregation.md`.

## 2026-06-12

## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface that Modal overrides. The Lima backend's now-unused `del`-of-`is_for_host_creation` is removed. No behavior change.

## 2026-06-10

Raised the stale coverage floor from 50% to 65% to match the coverage CI already measures (~67%).

## 2026-06-09

Offline hosts produced by this provider are now readable: the offline-host
construction path (used by both `get_host` for stopped hosts and
`to_offline_host`) returns an `OfflineHostWithVolume` (which implements the new
`HostFileReadInterface`) via the shared `make_readable_offline_host` helper.
This makes a stopped host's files readable through the same interface as an
online host -- used by Claude session preservation when a host is destroyed
while offline (the destroy path obtains the host via `get_host`), and available
to other readers of offline host data. The host's volume is resolved lazily on
first read, so this adds no per-host probe to host discovery. When no volume is
available, reads behave as "nothing there".

# Lima provider: run agents directly in the VM (drop docker-in-VM)

Removed the Lima provider's `is_host_in_docker` mode entirely. The Lima provider
no longer runs a Docker daemon, builds an image, or runs the agent inside a
nested container in the VM. Agents always run directly in the Lima VM.

- Added `is_run_as_root` to the Lima provider config. When enabled, mngr runs the
  agent in the VM as root (uid 0) -- so a coding agent can `apt install` and
  write anywhere with no `sudo`, exactly as it can inside a docker/VPS container.
  mngr injects a root client key, enables key-based root login, and SSHes in as
  root.
- `is_run_as_root=true` requires the btrfs additional-disk layout
  (`is_host_data_volume_exposed=false`); the invalid combination with the 9p
  bind-mount layout is rejected at config construction.
- Removed the docker-mode config fields (`is_host_in_docker`, `container_ssh_port`,
  `default_image`, `builder`, `docker_install_timeout`,
  `container_ssh_connect_timeout`, `image_build_timeout_seconds`,
  `default_container_run_args`, `docker_runtime`, `install_gvisor_runtime`).
  Configs that still set them now fail to load.
- Existing docker-mode Lima hosts (records with `is_host_in_docker=true`) are no
  longer startable; destroy and recreate them.
- The Lima provider no longer depends on `mngr_vps_docker`.

Consistent dependency setup across providers is now achieved by having the
project ship idempotent setup scripts that its `Dockerfile` runs (for the
docker/vps_docker/ovh providers) and that the Lima host runs directly after the
project is synced in. btrfs-based backups continue to work because `host_dir`
stays on a btrfs disk and the root agent can snapshot it directly.

## 2026-06-08

- The Lima VM now installs a pinned Docker Engine version from Docker's official
  apt repo (the same version the remote VPS providers use) instead of Debian's
  unpinned `docker.io` package, so workspace hosts run an identical, reproducible
  Docker regardless of provider.

Added `docker_runtime` and `install_gvisor_runtime` options to the lima provider config (used in `is_host_in_docker` mode). `docker_runtime` (default unset) passes `--runtime=<value>` to the agent container's `docker run` inside the VM. `install_gvisor_runtime` (default false) makes the VM provisioning install and register the gVisor `runsc` runtime with the in-VM Docker daemon via gVisor's official APT repository; idempotent and a no-op when runsc is already present. Installing is independent of enabling -- set `docker_runtime = "runsc"` to run the agent container under gVisor. Both options are ignored when `is_host_in_docker` is false (no container is run).

Made Lima host creation tear down half-built VMs on any failure. Both `create_host` and the docker-mode `_create_docker_host` now use a success-flag + `finally` so the VM and its btrfs additional disk are always cleaned up (and a failed-host record written) when creation does not complete -- including failures that are not `MngrError`/`OSError` (e.g. concurrency-group errors, timeouts, or interrupts) which previously escaped the `except` clause and left an orphaned, untracked VM behind. The docker-mode path also drops the container's forwarded-port `known_hosts` entry on cleanup.

`discover_hosts` now warns about orphaned Lima VMs: prefix-matched instances that no host record claims (leftovers from an interrupted create) are logged with the manual `limactl delete --force <name>` cleanup command, since mngr can neither manage nor garbage-collect a VM that has no record.

Added an opt-in `is_host_in_docker` mode to the Lima provider
(`providers.lima.is_host_in_docker`, default `false`). When enabled, the agent
runs inside a Docker container *in* the Lima VM (built from the project's
Dockerfile, exactly like the docker/vps_docker providers) instead of directly
in the VM. mngr treats the container as the host: ssh and all agent work happen
inside it, and Lima forwards the container's sshd out to the host's localhost.

The mode forces the in-VM btrfs additional-disk layout
(`is_host_data_volume_exposed` must be `false`): a per-host btrfs subvolume on
that disk backs the container's `host_dir`, and the `mngr_vps_docker` snapshot
helper is installed in the VM so the in-container agent can trigger consistent
`btrfs subvolume snapshot` backups (same `/mngr-snapshot` / `/mngr-snapshots`
contract as the other docker providers). `mngr stop` powers off the whole VM;
`start` boots it and relaunches the container; `destroy` removes the VM and the
disk. Default (`is_host_in_docker=false`) behavior is unchanged.

Standardized this plugin's test setup on `register_plugin_test_fixtures(globals())`
instead of `pytest_plugins = ["imbue.mngr.conftest"]`, so HOME isolation is wired
the same single way across all mngr plugins. Internal test-infrastructure change
only; no user-facing behavior change.

Switched the default Lima VM image from Ubuntu 24.04 to a pinned Debian 12
"bookworm" genericcloud image (both `aarch64` and `x86_64`). Now that the agent
typically runs inside a Docker container in the VM (`is_host_in_docker`), the VM
only needs Docker + btrfs + sshd, so a lighter base suffices; this also mirrors
the OVH provider's Debian 12 base. The provisioning script is apt-based and works
on Debian unchanged. Override per-arch via `providers.lima.default_image_url_*`.

Format and mount the per-host btrfs data disk in-guest during provisioning,
instead of relying on Lima's guestagent to auto-format it at boot. Minimal cloud
images (the new Debian genericcloud default) ship no `mkfs.btrfs`, so Lima could
not format the `format: true` btrfs additionalDisk -- it left the disk
unformatted and nothing mounted at `/mnt/lima-<name>`, which broke the per-host
subvolume creation (`cannot access '/mnt/lima-...-data'`) in both Lima btrfs
modes (docker-in-VM and direct-in-VM with `is_host_data_volume_exposed=false`).
The provisioning script now installs `btrfs-progs`, formats the data disk if it
is not already btrfs (idempotent; existing snapshot data survives), and mounts it
at the canonical path before the subvolume is created. On later boots Lima's
guestagent handles the mount (`btrfs-progs` now persists in the image).

Added `providers.lima.default_container_run_args` (default empty): extra
arguments appended to the `docker run` that starts the agent container in
`is_host_in_docker` mode. This is the only config path for injecting inner-
container `docker run` flags on Lima (the lima template's `start_arg` maps to
`limactl` VM args, not the container). Pairs with `docker_runtime="runsc"` to run
the agent container under gVisor -- e.g. set it to
`["--workdir=/", "--security-opt=no-new-privileges"]`, the same hardening the
docker provider applies, where `--workdir=/` avoids runsc aborting when the image
WORKDIR (inside the mounted volume) already exists as the process cwd.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-02

Collapsed a redundant `except` clause: `except (LimaCommandError, MngrError, OSError)` is now
`except (MngrError, OSError)` (since `LimaCommandError` is already a `MngrError` subclass). No
behavior change.

## 2026-05-29

Added an opt-in btrfs host-data volume mode to the Lima provider. The
new `is_host_data_volume_exposed: bool = True` field on `LimaProviderConfig`
(and the matching field persisted on `LimaHostConfig` in the per-host
record) controls how `host_dir` is backed:

- `True` (default) keeps today's behavior: `host_dir` is a 9p bind mount
  of `~/.mngr/providers/lima/<name>/volumes/<host_id>/` from the host
  machine. The host can read `host_dir` contents directly even while
  the VM is stopped, and `get_volume_for_host()` returns a usable
  `HostVolume`.

- `False` attaches a Lima-managed btrfs `additionalDisk`
  (`mngr-<host_id_hex>-data`, 100GiB default logical size, qcow2 sparse
  storage under `~/.lima/_disks/`) and symlinks `host_dir` directly to
  Lima's auto-mount path for that disk (`/mnt/lima-<disk_name>`); the
  9p mount is omitted entirely. This makes `host_dir` snapshottable as
  a single consistent btrfs filesystem. `get_volume_for_host()` returns
  `None` in this mode; callers (events API, mngr_claude session
  preservation, mngr_tmr, mngr_file) already degrade gracefully.

The chosen value is locked on the per-host record at create time so
`stop_host` / `start_host` always replay the same layout. Records that
predate the field default to `True`, preserving today's behavior for
all existing Lima hosts. `destroy_host` and `delete_host` now also
remove the named Lima disk when a host was created in btrfs mode.

A new `host_data_disk_size` config field (default `"100GiB"`) and new
`limactl_disk_create` / `limactl_disk_delete` helpers in `limactl.py`
round out the plumbing. `create_host` pre-creates the named disk via
`limactl disk create` before starting the VM (Lima's `additionalDisks
+ format: true` only auto-formats an already-existing disk). The
provisioning script `chmod 0777`s the btrfs root after the bind-mount
so the Lima default non-root user can write to `host_dir` without
sudo. Snapshot/backup API support stays out of scope for this change
(`supports_snapshots` remains `False`).

User-visible: minds workspaces running on Lima hosts can now be backed up
off-site (restic) when a backup provider is selected at creation time; the
local btrfs snapshot path these hosts use is what the backup service reads
from.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.

## 2026-05-20

- `mngr_lima`: drop ssh-keyscan from the host-creation flow. Each Lima VM now gets a pre-generated ed25519 host keypair injected into the guest via the Lima provision script (which writes `/etc/ssh/ssh_host_ed25519_key{,.pub}`, removes other host-key types, and restarts sshd before `limactl_start_new` returns). The host machine writes the matching `known_hosts` entry atomically using the public key it already has on disk -- no scan, no `Broken pipe` race during VM bring-up, no TOFU. Mirrors `mngr_vps_docker`'s cloud-init-driven host-key injection pattern, adapted to Lima's `provision[mode=system]` surface (Lima's `UserData` Go struct doesn't expose top-level `ssh_keys`). Per-host keys and the matching `known_hosts` file live under `<provider-dir>/keys/hosts/<host_id>/` so each VM has an isolated identity (no shared `known_hosts` accumulating stale `127.0.0.1:<old-port>` entries across restarts); `delete_host` cleans up that directory. `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them: a user-supplied `provision:` (e.g. to install extra packages) is appended after mngr's, and a user-supplied `mounts:` is appended after the `/mngr` volume mount -- so mngr's load-bearing entries (host-key injection in `provision`, the `/mngr` mount) are preserved. Lima runs `provision[mode=system]` scripts in list order, so mngr's host-key swap runs before any user script.

- `mngr_lima`: switch the serial-log tailer to `tail -F`. The previous `tail --follow=name --retry` is GNU-only; BSD tail (macOS) rejects it with "unrecognized option" and exits immediately, silently losing the serial-log diagnostics during Lima VM boot. `tail -F` is portable: GNU's `-F` is documented as equivalent to `--follow=name --retry`, and BSD's `-F` is documented to wait for a non-existent file to appear and follow it on creation. Empirically verified on both platforms (GNU coreutils 9.4 in a Lima Ubuntu 24.04 guest, and macOS aarch64).

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Fix Lima provider to actually disable guest -> host port forwarding. The previous empty `portForwards: []` did not suppress Lima's auto-appended fallback rule, so guest sockets on any interface (e.g. `0.0.0.0:8082`) leaked to host loopback and collided across coexisting VMs. The provider now emits two ignore rules -- one for `guestIP: 0.0.0.0` (with `guestIPMustBeZero: true`) and one for `guestIP: 127.0.0.1` -- because empirical testing on Lima 2.1.1 showed user-supplied rules match the guest bind address literally and neither rule alone catches both cases. `merge_lima_yaml` locks `portForwards` against user `--file` overrides. SSH is unaffected -- Lima manages it through a separate top-level config.
