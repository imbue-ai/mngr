# Unabridged Changelog - mngr_vps_docker

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_vps_docker/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-13

Reworked the outer-side btrfs snapshot helper (`snapshot_helper.sh`) so vps-docker backups capture data on every cycle instead of only the first.

Previously the helper snapshotted into a single fixed path (`snapshots/current`), deleting and recreating it each cycle. Under gVisor (runsc) the container reads that path through the gofer, which caches a handle to the first subvolume it opened -- so after the first delete+recreate every snapshot read came back empty and restic backed up nothing.

The helper now creates each snapshot at a unique, caller-named path (`snapshots/<name>`), fails rather than overwriting on a name collision, and deletes old snapshots by name on request. Cleanup targets are validated to be a single path component (no `/` or `..`) so a malformed request can never escape the snapshots directory or touch the live subvolume. The inner `host_backup` service drives the new naming and garbage-collects old snapshots down to a retained count.

## 2026-06-12

Fixed `builder = "DEPOT"` builds, which were broken for all VPS backends (aws/vultr/ovh).
The depot CLI installs to `$HOME/.depot/bin/depot`, which is not on the non-interactive
shell's PATH, but `build_image_on_outer` invoked it by bare name (`depot build ...`),
failing with `bash: line 1: depot: command not found`. The CLI is now resolved at run
time: a `depot` already on PATH is preferred (so an existing install is respected),
otherwise it falls back to the installer's off-PATH default `$HOME/.depot/bin/depot`,
installing there only when nothing is found. The same resolved path drives both the
idempotent install check and the `depot build` invocation.

A second bug in the same path also blocked depot: `DEPOT_TOKEN` was forwarded via the
streaming SSH command's `env`, but env forwarding for compound commands was broken in
`mngr` core (see the `mngr` changelog) so the token never reached `depot build`
("missing API token"). Both are now fixed.

## AWS provider support: shared VPS-Docker base refactor

- **Parallel-SSH host-record discovery** lifted from `VultrProvider` into `VpsDockerProvider`. Subclasses now implement two small hooks: `_list_provider_vps_hostnames()` and `_fetch_provider_instances()`. The cache scaffolding for instance listings (`_instances_cache` field, `reset_caches` integration) lives in one place.
- **New `_validate_provider_args_for_create` hook** on `VpsDockerProvider` (default no-op), called by `_provision_vps` immediately before `create_instance`. AWS uses this for its pytest-time `auto_shutdown_minutes` guard.
- **`wait_for_instance_active` lifted onto `VpsClientInterface`** as a default method with a `slow_provisioning_warning_threshold_seconds` field for per-provider tuning. AWS / Vultr no longer duplicate the polling loop.
- **`VpsClientInterface.create_instance` `tags` parameter** widened to `Mapping[str, str]` for read-only-friendly call sites.
- **`os_id` removed from the shared interface**: `VpsClientInterface.create_instance` no longer carries the Vultr-specific image-selection int. `VpsHostConfig` / `ParsedVpsBuildOptions` / `VpsDockerProviderConfig` all lose the field. The `--vps-os=` / `--vps-image=` / `--vps-ami=` build args produce a dedicated error pointing at the per-provider config field that replaces them (`default_os_id` / `default_image_name` / `default_ami_id`).
- **Build-args prefix moved per-provider**: `--vps-region=` / `--vps-plan=` are gone. Each provider now uses its native prefix: `--aws-region=` / `--aws-instance-type=`, `--vultr-region=` / `--vultr-plan=`, `--ovh-datacenter=` (alias `--ovh-region=`) / `--ovh-plan=`. The dropped `--vps-*` prefix raises a migration error with the new name. `--git-depth=` stays shared (it's about the local mngr build context). The shared parser is now `parse_vps_build_args` (public) and takes `provider_prefix` + `plan_arg_name`; each provider overrides `_parse_build_args`. `default_plan` is dropped from `VpsDockerProviderConfig` (each provider's config carries its own native field: Vultr/OVH `default_plan`, AWS `default_instance_type`). `vps_boot_timeout` renamed to `instance_boot_timeout` to drop leaked "VPS" terminology now that hyperscalers (AWS, future GCP/Azure) are in scope.
- **New public `MinimalVpsDockerProvider`** in `mngr_vps_docker.instance`. Pairs with a `vps_client` whose provisioning calls raise (e.g. an `ExternallyManagedVpsClient` stub): provisioning is managed elsewhere and this provider only ever runs the post-provisioning host-setup machinery. Its `_parse_build_args` extracts `--git-depth=N` and forwards everything else to docker; the legacy `--vps-*` prefix is rejected with a migration error. Used by `mngr_imbue_cloud`'s slow path; available for any other caller that needs the same shape.
- **New composable parser helpers**: the shared `parse_vps_build_args` monolith is rebuilt on top of small composable pieces (`extract_single_value_arg`, `extract_git_depth`, `extract_presence_flag`, `raise_if_vps_migration_arg`, `raise_if_unknown_provider_arg`). `parse_vps_build_args` stays as a convenience for the region+plan+git-depth shape; providers with extra knobs (currently only AWS, which adds `--aws-ami=` and the presence-only `--aws-spot`) compose the lower-level helpers directly. `extract_presence_flag` covers boolean opt-in flags and rejects the value-bearing form (e.g. `--aws-spot=true`) so a likely typo fails fast. `VpsDockerProvider._parse_build_args` is now a real `@abstractmethod` (`ProviderInstanceInterface` already inherits `ABC`); the previous "raises a `must override` `MngrError`" pattern surfaced the contract only at runtime.
- **New `_create_vps_instance` hook** on `VpsDockerProvider`. The base `_provision_vps` calls it instead of `self.vps_client.create_instance(...)` directly. Default impl mirrors the previous call; AWS overrides to thread `ami_id_override` from `ParsedAwsBuildOptions` through to `AwsVpsClient.create_instance`'s new optional kwarg. Lets providers add per-call knobs without widening the shared `VpsClientInterface`. `_provision_vps` now takes `parsed: ParsedVpsBuildOptions` instead of pre-extracted `region` / `plan` (OVH's override updated accordingly).
- **New `auto_shutdown_minutes` field** on `VpsDockerProviderConfig`. Cloud-init schedules `shutdown -P +N` when set; on AWS, paired with `InstanceInitiatedShutdownBehavior=terminate`, the instance auto-terminates from the inside.
- `is_for_host_creation` flag removed; replaced with the default-no-op `bootstrap_for_host_creation` hook on `ProviderBackendInterface`. No behavior change for VPS-Docker subclasses.
- README updated and an out-of-place "OS image selection is provider-specific" block removed (it tried to document the dropped `--vps-os=` arg).
- **Cloud-init sshd bump uses a drop-in + reload instead of restart**. `MaxSessions` / `MaxStartups` is now written via cloud-init `write_files` to `/etc/ssh/sshd_config.d/99-mngr.conf` and applied with `systemctl reload ssh` (SIGHUP, no connection drop), instead of an in-place `sshd_config` rewrite + `systemctl restart`. The restart was tearing down in-flight SSH connections and hanging the provisioning poll loop on pyinfra's 10s per-command read timeout, which fired the EC2 lifecycle test failure.
- **`_wait_for_cloud_init` swallows transient `HostConnectionError` per poll** so the loop survives sshd reload windows; the outer `timeout_seconds` remains the hard wall. The body was extracted to a module-level `_wait_for_cloud_init_marker` helper with injectable clock / sleeper for unit testing.
- **Cloud-init installs Docker via the Debian `docker.io` package instead of `curl get.docker.com | sh`**. The packaged install runs inline with cloud-init's other apt packages (ca-certificates, curl, rsync) and finishes in ~5-15s on a `t3.small`, vs ~60-120s for the upstream installer script (which fetches the full docker-ce stack and configures Docker's own apt repo).
- After merging `main` (which raised `ty` to the stricter 0.0.39), the discovery test's `_DummyOuter` stand-in is now `cast` to `OuterHostInterface` at the `yield` site, matching the sibling vps_docker tests. Test-only.
- **`builder=DEPOT` without `DEPOT_TOKEN` now fails fast, before provisioning.** Previously a DEPOT build whose `DEPOT_TOKEN` was unset failed only at the build step -- after a billable VPS had already been provisioned and cloud-init had run. `create_host` now runs an `ensure_depot_token_available(...)` preflight up front (only when the create will actually build, i.e. non-empty docker build args; a plain image pull needs no token), raising the same actionable error before any instance is created. The build-time check remains as the last line of defense.

- **`auto_shutdown_minutes` renamed to `auto_shutdown_seconds`** on `VpsDockerProviderConfig`, for unit consistency with the rest of the config (everything else is seconds) and to sit alongside the existing seconds-based `default_idle_timeout`. It remains a hard max-lifetime cap (distinct from the activity-based idle timeout). cloud-init rounds the value up to whole minutes for `shutdown -P +N` (the granularity `shutdown` accepts), with a floor of 1 minute for any positive value. **Action required:** any `settings.toml` using `auto_shutdown_minutes` must switch to `auto_shutdown_seconds` and multiply by 60.

- `VpsClientInterface.wait_for_instance_active` now logs at debug level (instead of silently `pass`-ing) when an instance reports ACTIVE but has no IP yet and the poll is retried, so a stuck provision is traceable without spamming the happy path.

Fixed `mngr create` against the VPS Docker backends (aws/vultr/ovh) failing during the
post-build git seed with `remote rejected ... refusing to update checked out branch` when
the build context is a primary git checkout (`.git` is a directory) that has linked
worktrees -- e.g. running `mngr create -t aws` from a main checkout that keeps a worktree
per branch.

The remote-`docker build` flow now clones *any* local git context into a temp dir before
upload (previously only a linked worktree, whose `.git` is a gitlink file, or an explicit
`--git-depth`, triggered the clone). A fresh clone's `.git` is self-contained and carries
no `.git/worktrees/` admin, so it no longer baked the operator's other branches into the
image as "checked out" -- which is what made the mirror seed push refuse them. The
operator's working tree (including uncommitted edits) is still overlaid onto the clone, so
in-flight changes continue to reach the build.

## 2026-06-11

Test-quality cleanup of the mngr_vps_docker unit tests (no production code changed):

- `instance_test.py`: the two `_emit_docker_build_output` tests now capture log
  output and assert the BUILD-level line (stripped) is emitted for non-empty
  input and nothing is emitted for whitespace-only input, instead of only
  asserting "does not raise". The scattered `_is_retryable_rsync_error` cases
  were consolidated into a parametrized test covering one representative stderr
  string for each of the eight retryable connection patterns plus negatives.
- `_outer_helpers_test.py`: removed the duplicate `_redact_secret_env` /
  `_is_retryable_rsync_error` tests (now covered once, comprehensively, in
  `instance_test.py`) and their unused imports.
- `_snapshot_helper_test.py`: the snapshot_helper.service load test now asserts
  the resource is non-empty and contains expected systemd directives rather than
  discarding the result.
- `cloud_init_test.py`: replaced the loose bag-of-substrings generation checks
  with a single full `inline_snapshot` of the rendered user_data, so the
  load-bearing YAML indentation and key placement (the embedded SSH private key
  in particular) are pinned exactly, plus a companion test that parses the
  output as YAML and asserts the private key lands at the correct nesting.
- `host_store_test.py`: `test_list_persisted_agent_data_reads_all_agents_in_one_round_trip`
  now asserts the read call count does not grow with agent count (2 vs 5) rather
  than pinning a bare literal, and documents that the call-count assertion
  deliberately guards the network round-trip budget. Removed two tautological
  constructor round-trip tests.
- `config_test.py` / `primitives_test.py`: removed tautological constructor
  round-trip tests; the remaining default/wire-value contract tests carry a
  comment marking them deliberate change-detectors.
- `test_ratchets.py`: tightened the `init_methods_in_non_exception_classes`
  ratchet from 1 to 0 (the recorded count was stale; actual is 0), and bumped
  `yaml_usage` to 3 for the cloud-init YAML-parse test above (the ratchet
  prevents introducing new YAML config, not parsing the cloud-init YAML format
  we are forced to emit).

## 2026-06-10

Raised the stale coverage floor from 40% to 45% to match the coverage CI already measures (~48%).

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

## 2026-06-08

Consolidated host-level provisioning into a single source of truth. A new
`host_setup.py` module defines the ordered, idempotent, config-gated setup steps
(pinned Docker install, optional gVisor `runsc` install, sshd `MaxSessions` /
`MaxStartups` tuning, base packages, and an optional qemu purge). `cloud_init.py`
now renders its first-boot `runcmd` block from those same steps, and a new
`apply_host_setup_on_outer()` runs the identical steps over SSH so a host can be
re-provisioned consistently after first boot.

Docker is now pinned to an exact version (29.5.1 on Debian 12) and installed via
the official Docker apt repo with `--allow-downgrades`, so provisioning is
reproducible and a re-run upgrades/downgrades an old host to match (replacing the
unpinned `get.docker.com | sh` install). gVisor `runsc` is pinned to a dated
release and downloaded + checksum-verified directly.

The SSH host-key injection stays first-boot-only in the cloud-init wrapper and is
deliberately excluded from the re-runnable steps, so re-provisioning never resets
the VPS host key or breaks `known_hosts`.

Made `start_container` (shared by vps_docker / ovh / lima) resilient to restarting
a container under gVisor (runsc). A leftover runsc sandbox from the container's
previous run can keep the rootfs-overlay `.gvisor.filestore` mounted, so
`docker start` fails with "repeated submounts are not supported with overlay
optimizations". `start_container` now runs the start + recovery + retry as a
single remote script: on that specific gVisor error it reaps the leftover runsc
processes scoped to that container id, removes the stale on-disk filestore, then
retries. A normal start stays a single `docker start`.

Fixed the docker-on-VPS/lima build-context upload to pass the SSH port (`-p <port>`) to rsync's ssh transport. Previously `build_ssh_transport_for_outer` dropped the port, so uploads always targeted port 22 -- fine for VPS (sshd on 22) but broken for lima docker-mode, where the VM's sshd is reached via a Lima-forwarded port on 127.0.0.1, causing "No ED25519 host key is known for 127.0.0.1" / host key verification failures.

Added `ContainerSetupError` (a `MngrError` subclass) and a `translate_outer_concurrency_errors` boundary context manager in `container_setup`. The outer-host build/upload/snapshot-helper helpers run their work inside `ConcurrencyGroup`s, so failures surfaced as raw `ConcurrencyExceptionGroup` / `ProcessTimeoutError` -- neither a `MngrError` -- and slipped past provider `except MngrError` cleanup clauses, leaking half-built hosts. These failures are now re-raised as `ContainerSetupError` (preserving the cause), so provider create paths catch and clean them up. Wired into `build_image_on_outer_from_build_args` (clone + upload) and `provision_snapshot_helper_on_outer`.

Extracted the reusable docker/btrfs/snapshot-helper/image-build helpers out of
`VpsDockerProvider` into a new `imbue.mngr_vps_docker.container_setup` module
with public names (e.g. `run_container`, `provision_snapshot_helper_on_outer`,
`prepare_btrfs_on_outer`, `setup_container_ssh`,
`build_image_on_outer_from_build_args`). `VpsDockerProvider` now imports them,
and the `_setup_container_ssh` / `_build_image_on_vps` methods delegate to the
shared functions. No behavior change for VPS Docker hosts; this is the shared
toolkit the Lima provider's new docker-in-VM mode builds on.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-03

Refactored `VpsDockerProvider.create_host` so the post-ordering work (container
build/run, SSH setup, certified-data + host-record finalize) lives in a single
public method, `create_host_on_existing_vps`, that operates over a caller-supplied
outer SSH connection and makes no VPS-API (ordering) calls. `create_host` now
orders the VPS and then calls it, so there is exactly one "set up the host after
the VPS exists" code path.

Added `teardown_container_on_existing_vps` to remove a host's container + per-host
btrfs subvolume + named volumes on an already-reachable VPS (no VPS-API calls),
for rebuilding a container in place.

Added `ExternallyManagedVpsClient`, a `VpsClientInterface` stub for providers that
operate on a VPS they did not order (e.g. an imbue_cloud-leased pool host); every
ordering/snapshot/ssh-key call raises so a wrong call site fails loudly.

These are consumed by `mngr_imbue_cloud`'s new slow path; existing OVH/Vultr
behavior is unchanged.

## 2026-06-02

Simplified exception handlers now that `HostError`/`HostConnectionError` are `MngrError`
subclasses: the redundant `except (HostConnectionError, MngrError)` guards in the VPS Docker
instance are now just `except MngrError`. No behavior change -- host connection errors are
still caught and handled the same way.

## 2026-06-01

# Offline agent field generators

Updated the provider's `get_host_and_agent_details` override to accept and forward the new `offline_field_generators` parameter to the base implementation, so offline plugin fields (see the mngr changelog entry) are populated when a host falls back to offline data.

## 2026-05-29

User-visible: minds workspaces running on docker-on-VPS hosts can now be
backed up off-site (restic) when a backup provider is selected at creation
time; the outer-trigger btrfs snapshot path these hosts use is what the
backup service reads from.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

Provisioned a per-host outer-side btrfs snapshot helper for the new
forever-claude-template `host_backup` service. Each vps-docker host now
gets:

- `/usr/local/sbin/snapshot_helper.sh` + `snapshot_helper.service` (a
  systemd unit shipped as a bundled resource in
  `imbue/mngr_vps_docker/resources/`) that watches a per-host docker
  volume `mngr-snapshot-trigger-<host_id_hex>` for `request.json` files
  and produces matching `result.json` files describing the outcome of
  `btrfs subvolume snapshot` / `btrfs subvolume delete` against the
  per-host subvolume.
- That docker volume is mounted into the agent container at
  `/mngr-snapshot/` so the in-container `host_backup` script can do the
  RPC; the outer's `<btrfs-mount>/snapshots/` directory is bind-mounted
  read-only into the container at `/mngr-snapshots/` so restic can read
  the snapshot the helper produced.
- Cloud-init now installs `inotify-tools` and `jq` so the helper has
  what it needs at boot.
- `destroy_host` removes the per-host snapshot-trigger volume alongside
  the existing host-volume cleanup.

The per-host unified docker volume on Vultr / OVH VPSes is now backed by a btrfs
subvolume on a loop-mounted btrfs filesystem on the VPS, so the host's agent
data is eligible for consistent `btrfs subvolume snapshot -r` snapshots.

Concretely, `VpsDockerProvider._setup_container_on_vps` now begins by calling a
new `_prepare_btrfs_on_outer` step that, idempotently and on demand, installs
`btrfs-progs`, `fallocate`-allocates `/var/lib/mngr-btrfs.img` (sized to the
outer's free space minus a configurable reservation), `mkfs.btrfs`'s it,
loop-mounts it at `/mngr-btrfs`, persists the mount in `/etc/fstab`, and
creates a per-host subvolume at `/mngr-btrfs/<host_id_hex>`. The unified
docker volume (`mngr-host-vol-<host_id_hex>`) is then created with
`--driver=local --opt type=none --opt device=/mngr-btrfs/<host_id_hex> --opt o=bind`,
so its real on-disk storage is the btrfs subvolume; `host_store.py` reads the
bind-source path out of `Options.device` instead of the docker-managed
`Mountpoint`. `destroy_host` runs a best-effort `btrfs subvolume delete`
immediately before removing the docker volume (VPS-destroy nukes the loop file
otherwise).

Docker itself still uses default `data-root=/var/lib/docker` and
`storage-driver=overlay2` on the ext4 root; only this one volume's storage is
on btrfs. Three new fields on `VpsDockerProviderConfig` make the layout
configurable: `btrfs_mount_path` (default `/mngr-btrfs`),
`btrfs_loop_file_path` (default `/var/lib/mngr-btrfs.img`), and
`outer_disk_reserved_gb` (default 20).

**Breaking change:** existing vultr / ovh hosts created on the prior
plain-`docker-volume-create` layout cannot be discovered or managed after
upgrade. Destroy and recreate them.

Consolidated the `docker_vps` provider's two-volume layout (per-user state container
volume + per-host data volume) into a single per-host Docker volume on the VPS. The
unified volume `mngr-host-vol-<host_id_hex>` now holds `host_state.json`,
`agents/<agent_id>.json`, and `host_dir/` side by side, mounted at `/mngr-vol` inside
the agent container with `/mngr` symlinked to `/mngr-vol/host_dir`. mngr now reads
and writes metadata directly on the VPS filesystem via the volume's docker mountpoint
(discovered with `docker volume inspect`); the dedicated Alpine "state container" and
the per-user `docker-state-<user_id>` volume are no longer created or read.

This makes future single-volume backup of a host straightforward (one
`docker run --rm -v <volume>:/data ...` captures everything) and removes a layer of
indirection that existed only for historical symmetry with the local `docker` provider.

**Breaking change:** existing `docker_vps` hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.

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

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

`rsync` added to `mngr_vps_docker.cloud_init.generate_cloud_init_user_data`'s
package list for belt-and-suspenders symmetry on cloud-init backends (paired
with `mngr_ovh`'s `install_required_outer_packages` on the non-cloud-init OVH
path).

- Refactors `VpsDockerProvider` to lift the shared parallel-SSH discovery into the base class behind a new `_list_provider_vps_hostnames()` seam method (concrete in the base, returns `[]`; overridden by concrete providers); `mngr_vultr` now only contributes the tag-listing.
- Widens `os_id` in the VPS Docker base to `int | str` so providers (like OVH) can carry friendly image names through the existing build-args parser without disrupting integer-id providers (like Vultr).
