# Unabridged Changelog - mngr_lima

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_lima/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-23

Lima VMs upgrade `ca-certificates` at provisioning (one best-effort attempt, so an offline start is not held up) instead of only installing it when missing, so a VM built from an older cloud image picks up the fix for the compromised `emSign ECC Root CA - G3` root.

## 2026-10-18

The Lima provider reports its create steps through mngr's structured progress stream (`--format jsonl`): `lima.start_vm`, `lima.wait_cloud_init`, and `lima.wait_ssh` under the create's `creating_host` phase. The VM start advances through limactl's own "requirement k of n" counter as the guest boots.

## 2026-10-17

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`).

- The per-VM key layout (`keys/hosts/<host id>/` holding `ssh_host_ed25519_key` and `root_ssh_key` with their public halves and the VM's own `known_hosts`) is published as `LIMA_HOST_ACCESS_MANIFEST` (`constants.py`, with the key-file names `LIMA_HOST_KEY_NAME`, `LIMA_ROOT_SSH_KEY_NAME` and `LIMA_KNOWN_HOSTS_NAME`), which the provider's own key dirs resolve through, so the minds workspace record carries a VM's access material without restating the layout.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-12

Lima discovery now reports each host's recorded size (`DiscoveredHost.size`: the VM's CPUs and memory, and the data disk that backs the host's data) from the host records it already reads, so the `mngr observe` stream carries it.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-30

Removed the 10 comment lines that had accumulated inside `libs/mngr_lima/imbue/mngr_lima/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

## 2026-09-29

The library default Lima guest image is now the Debian 13 "trixie" genericcloud release `20260722-2547`, served from imbue's public artifact mirror (`apt.imbuepackages.com/artifacts/debian-cloud-image/`) instead of the Debian 12 `20260601-2496` release on `cloud.debian.org`, which prunes old releases and would eventually break every create pinned to one. The `default_image_url_aarch64` / `default_image_url_x86_64` provider config fields still override it.

The lima provider now records, reports, and can change a host's size, and its VMs keep their boot disk from filling.

- `mngr list` reports a lima host's real size as `host.resource`: the `--cpus` / `--memory` its VM was created with (else the instance config's values, else lima's own defaults) and the size of the btrfs data disk that backs its data (the boot disk on the exposed layout), instead of the old 4 CPU / 4 GiB / 100 GiB placeholder. The data-disk size is now recorded on the host record; `host_data_disk_size` is validated as a lima size string.

- New `mngr lima resize HOST [--cpus N] [--memory GIB] [--disk GIB]` rewrites the recorded size. A stopped VM is reconfigured at once (`limactl edit` for CPUs and memory, `limactl disk resize` for the data disk); a running VM only has the size recorded, and `mngr start` always brings the VM up to the recorded size before starting it. A data disk never shrinks, and a host on the exposed bind-mount layout has no data disk to resize.

- Every VM's provisioning now caps the systemd journal at 512 MiB (the same cap the VPS and gen-2 hosts get) and, in the btrfs layout, grows the data filesystem to fill its disk on every boot, so a grown disk is usable on the next start.

- `mngr create`'s start-args help says which disk `--disk` is (the boot disk) and points at `mngr lima resize`.

Merges the Debian 13 default (issue #975): the library default Lima guest image is the Debian 13 "trixie" genericcloud release `20260722-2547` served from imbue's artifact mirror, the same release the default-workspace-template pins. See the `mngr-host-sizing-debian-13` entry for the details.

## 2026-09-25

Removed the two helpers that only the retired slice-fleet key repair used: `patch_root_authorized_keys_block_in_lima_yaml` (the in-place rewrite of a stored `lima.yaml`'s truncating root-key step) and the public `build_root_authorized_keys_block`, which is now private to the yaml generator. The desktop lima provider itself is unchanged.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-08-25

Comment-only terminology sweep: mngr-level prose speaks host/agent instead of the minds-level workspace vocabulary.

Host-record reads now distinguish "unreadable" from "absent": parse failures are retried (a torn read of a mid-write record heals on re-read), and after retries either warn-and-treat-as-missing (default) or raise `HostRecordUnreadableError` when the new off-by-default `strict_host_record_parsing` mngr config flag is enabled. Lima also gains discovery-time healing of its persisted agent store (records for live agents missing from the store are persisted; orphaned records are removed), matching docker. Part of the robustness fixes from the 2026-07-14 partial workspace-destroy incident.

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-19

Doc-only: two `lima_yaml.py` docstrings now attribute the slice key-repair sweep to `minds-admin repair-keys` (the sweep moved from `mngr imbue_cloud admin repair-keys` to the private minds-admin operator CLI). No behavior change.

## 2026-08-18

Update a comment reference for the relocation of minds' operator docs to `apps/minds/docs/deploy/` (`lima_yaml.py`).

No behavior change. Documented and regression-tested that the Lima provision script must reinstall the pre-injected sshd host key on every boot: lima regenerates cidata with a fresh instance-id on every `limactl start`, so cloud-init replays its per-instance `ssh` module each boot, which (with the default `ssh_deletekeys: true`) deletes and regenerates the sshd host keys -- the final-stage provision rewrite is what restores the pinned key. A run-once marker guard around the install was tried during this release and reverted after a live VM restart test showed it leaves the VM serving an unpinned, freshly generated key that mngr's strict host-key pinning refuses. Host keys rotated after first boot (imbue_cloud slice adoption) are instead preserved by the adoption reconciler, which this release fixes to actually run at boot (see the mngr_imbue_cloud changelog).

## 2026-08-16

Fixed pre-per-host-key lima workspaces being unreachable through the forward SSH tunnel from this release's strict host-key checking: when a VM only authorizes the legacy provider-wide root client key, that keypair is now symlinked into the host's own keys directory on first resolution, so the key's sibling `known_hosts` is the per-host pin file lima maintains. Previously the legacy key's directory had no pin file at all and the tunnel refused to connect, which the desktop client then misreported as a sick workspace (see mngr-internal#427 for the reporting follow-up).

## 2026-08-15

Phase 2 of the user-controlled-keys plan: per-host root client keys for Lima VMs.

New run-as-root VMs authorize a root client key minted under the host's own keys dir instead of the provider-wide `root_ssh_key`; older VMs keep working via a read fallback to the legacy shared pair. This keeps provider-wide key material out of anything derived per host (e.g. a future synced workspace record).

The per-host known_hosts file is now rendered through mngr core's new host-key pin store via a sole-endpoint pin, preserving the existing behavior that the file reflects only the VM's current forwarded port.

The cidata provisioning script's root `authorized_keys` step now appends its own line if absent instead of truncating the file (re-landed from PR 382). Lima replays provisioning on every VM start, so the old truncating `cat >` silently dropped every key added after the carve -- most damagingly the imbue_cloud connector's lease-time injection of the owner's key, locking them out of the VM for good while the workspace kept working over the container's separate sshd.

New `patch_root_authorized_keys_block_in_lima_yaml`: rewrites an existing VM's stored `lima.yaml` from the historical truncating form to the appending one, used by the imbue_cloud `admin repair-keys` fleet sweep to fix slices carved before this change.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-31

Lima guest provisioning now disables sshd's per-source penalties (`PerSourcePenalties no`) on guests whose sshd supports the option (OpenSSH >= 9.8, e.g. Debian 13 images). Under qemu user-mode networking every host-side connection reaches the guest from the single NAT gateway address, so one authentication timeout during a CPU-starved boot penalized ALL of mngr's connections at once and reconnect attempts kept the penalty window alive indefinitely -- leaving a perfectly healthy VM permanently unreachable ("waiting for system interface to be ready" forever in minds). The option is written only when the running sshd understands it, since unknown keywords are fatal to older sshds. Slice VMs share this provisioning path and get the same fix.

## 2026-07-28

Lima hosts now record the `host_dir` they were created with, and reads target that recorded value instead of whatever `host_dir` the current context's provider config resolves to.

Without this, a Lima host created from a repo whose `.mngr/settings.toml` customizes `[providers.lima].host_dir` was unreadable from anywhere else. minds hits this on every workspace: it runs `mngr create` with the workspace clone as cwd (so the project setting applies and the host is built at `/home/user/.mngr`), but runs `mngr forward` and the per-agent `mngr event ... --follow` streams from `$HOME`, where no project settings apply and the provider falls back to the built-in default `/mngr` -- a path that does not exist in the VM. The events stream exited non-zero, `mngr forward`'s resolver never learned the agent's `system_interface` URL, and the plugin served a permanent 503, so the desktop app's readiness probe spun until it timed out.

The same recorded value now also drives the in-VM activity watcher and the plain-text service log directory. Restarting a host from a context that resolved a different `host_dir` previously installed the watcher against that other path: it found no `data.json` there, so the idle-timeout and max-age checks were skipped and the host stopped idle-stopping -- silently, because a failed watcher start is not raised. `_host_log_dir_str` takes the host's effective `host_dir` for the same reason, so `<host_dir>/logs` lands beside the data the host actually uses.

The docker provider already persisted its per-host `host_dir` for exactly this reason; lima now matches. Records written before this field existed carry no `host_dir` and keep falling back to the provider instance's configured value, so existing hosts are unaffected.

A ratchet (`test_project_ratchets.py`) caps the provider instance's ambient `host_dir` reads at the three that belong in `create_host`, so a future read path cannot silently reintroduce the same class of bug.

## 2026-07-26

Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the new optional `volume_home_path` config lets the btrfs data disk back a whole home tree (e.g. `/home/user`) with `host_dir` inside it, and plain-text service logs honor the new `host_log_dir` config. Defaults unchanged.

Reliability fixes for Lima hosts, from the mngr-internal#121 investigation:

- Creating a Lima host with a name that is already in use now fails fast with `HostNameConflictError` (naming the existing host and its state) instead of silently building a second VM under the same name. The name is reserved with a BUILDING host record before `limactl start`, so concurrent creates conflict too; FAILED and DESTROYED records do not conflict, so a name can be reused after a failed create. A stale reservation from a hard-killed create can be cleared with `mngr destroy @<host-id>` (the conflict error says so).

- `delete_host` now deletes the Lima VM definition itself (tolerating it already being gone) before removing the disk and records, so `mngr gc`'s offline-host path no longer strands VMs in `limactl list` with nothing left to manage them.

- The in-guest btrfs format now applies the `lima-<disk_name>` filesystem label Lima's own `05-lima-disks.sh` probes for, and heals the label on disks formatted before this fix. Previously the missing label made Lima re-enter its first-time disk setup on every boot -- re-running `sfdisk` against the data disk and failing `cloud-final`, leaving bare-image VMs systemd-`degraded` on every boot. The failure is now confined to the first boot on a stock image (where nothing can preinstall `btrfs-progs` ahead of Lima's boot scripts).

- A transient network failure while downloading the base image (TLS handshake timeout, connection reset, 5xx, ...) now retries `limactl start` once, cleaning up the half-created instance between attempts. Permanent failures (404/403) are never retried.

New optional `volume_home_path` config: the btrfs data disk can back a whole home tree (e.g. `/home/user`, with `host_dir` mkdir'd inside it on the disk) instead of symlinking `host_dir` directly. Requires the btrfs additional-disk layout; validated so `host_dir` sits strictly inside the configured path. Plain-text service logs honor the new `host_log_dir` config. Defaults unchanged (including the bookworm default guest image).

## 2026-07-24

Lima's `"Unknown"` instance status (and any unrecognized status string) now maps to `HostState.UNKNOWN` instead of `CRASHED`. limactl reporting that it could not determine a VM's state is non-evidence about the VM, and `CRASHED` triggers unattended auto-restarts in consumers (e.g. the minds recovery page) and makes the host GC-eligible. `"Broken"` still maps to `CRASHED` -- that is limactl positively reporting breakage.

## 2026-07-14

Fixed: a `limactl` invocation that fails at runtime (e.g. crashes at startup) is now reported correctly instead of leaking an opaque error. Every `limactl` wrapper helper (`list`, `stop`, `delete`, `disk create`, `disk delete`, `show-ssh`, `shell`, `start`, and `--version`) funnels through a single runner that translates the process runner's raw `ProcessError` into the intended `LimaCommandError` -- previously each helper disabled the runner's exit-code check and re-tested the return code by hand, which was dead code because the runner had already raised, so the error slipped past callers that catch `LimaCommandError`. Consolidating the translation makes it impossible for a new helper to reintroduce that leak, and the wrapped error now preserves the failed command's stdout as well as its stderr. As part of this, `limactl shell` (used to wait for cloud-init during host creation) now raises `LimaCommandError` when limactl itself cannot run the command, rather than silently returning a non-zero exit code that the caller ignored -- so a broken VM fails host creation with a clear error instead of proceeding.

Changed: when `limactl` is installed and new enough but an invocation fails at runtime, Lima discovery now reports the provider as unavailable (`Provider 'lima' is not available: ...`), matching how other providers report an unreachable backend, and gets the same retry/backoff -- instead of silently marking every Lima host offline. A genuinely absent or too-old `limactl` still degrades gracefully to a local, all-offline host view.

## 2026-07-13

Creating a Lima VM no longer fails with a `UNIX_PATH_MAX=104 characters` error when your home path and mngr prefix are long. Lima names each VM's SSH socket after the instance name and rejects names that push that socket path over the OS limit; the instance name (`<prefix>host-<32-char id>`) could exceed it for longer usernames on longer-prefixed environments (e.g. `minds-staging-`). The random id portion is now shortened just enough to keep the socket path within the limit, leaving the name unchanged when it already fits. If the prefix plus `LIMA_HOME` leave no room at all, creation now fails early with a clear message instead of a cryptic limactl fatal error.

## 2026-07-10

Fixed a Lima workspace-creation failure that surfaced as a confusing `SSH host key error (Host key for 127.0.0.1 does not match.)`.

The VM provisioning script installed the pre-trusted SSH host key *after* a network-dependent `apt-get install`, all under `set -e`. A transient Debian mirror hiccup (`apt-get` failing to fetch package indexes) would abort the whole script before the host key was installed, so the VM kept its default host key while mngr had already pinned the key it expected -- causing a strict host-key-check mismatch on connect.

The host-key swap (and sshd tuning) now runs first, before any package fetch, so SSH host-key trust no longer depends on the network. The package install is additionally wrapped in a retry loop to ride out transient apt mirror failures; if it still fails, the error now surfaces clearly on an SSH-reachable host instead of as a host-key mismatch.

## 2026-07-06

Lima hosts can now be renamed. `LimaProviderInstance.rename_host` updates the logical host name on the host record (a local, offline-writable store), so it works whether or not the VM is running.

New Lima VMs now derive their limactl instance name from the immutable host id (`<prefix><host_id>`) rather than the host name, so a rename never leaves the VM's instance name out of sync with the host name (limactl has no native rename). Existing VMs created under the old `<prefix><host_name>` scheme keep working unchanged, since discovery reads the host name and instance name from the persisted record rather than parsing the instance name.

Integrates the "simple names" work: Lima hosts can now be renamed (`LimaProviderInstance.rename_host` updates the offline-writable host record), and new Lima VMs derive their limactl instance name from the immutable host id (`<prefix><host_id>`) so a rename never desyncs the instance name from the host name. Existing VMs created under the old scheme keep working.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

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
