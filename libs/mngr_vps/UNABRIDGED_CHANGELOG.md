# Unabridged Changelog - mngr_vps_docker

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_vps_docker/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-22

Test-only: the test stand-ins for the host file reader accept the new optional `max_byte_count` argument of `read_file_tail_from_offset`.

## 2026-10-20

The discovery test's canned outer now recognizes the outer listing script by its label lookup rather than by its first line, which the script no longer opens with (it first inspects the container a previous listing found).

Configuring a host's sshd now logs, at info, the fingerprints of the keys that are about to replace the host's `authorized_keys` (the client key and any appended entries), along with the host id, provider and profile. A later key rejection can then be matched against the keys the host was last configured with.

## 2026-10-18

The VPS-backed providers (AWS, GCP, Azure, Vultr, and the Imbue Cloud slow path, which all share this machinery) report their create steps through mngr's structured progress stream (`--format jsonl`): `vps.create_instance`, `vps.wait_active`, `vps.wait_ssh`, `vps.wait_bootstrap`, then `vps.build_image` or `vps.pull_image`, `vps.start_container`, and `vps.wait_agent_ssh`, all under the create's `creating_host` phase. A remote image build advances through its Dockerfile stages by parsing the BuildKit stage markers streamed back from the machine.

## 2026-10-17

The VPS-root client key (`vps_ssh_key`) is now minted per host under `keys/host_keys/<host-uuid-hex>/` instead of once per provider instance, so a host's per-host key directory is the complete set of material that opens it (VPS-root key, container key, both sshd host keys) and no provider-wide key ever needs to leave the machine that created a host. Discovery reads each instance's host id from the cloud listing (`mngr-host-id`) and opens the VPS with that host's own key; an instance without a parseable host id is skipped with a warning.

Read paths never create keys any more: an operation on a host whose per-host key is absent fails with `VpsHostKeyMissingError` before any SSH attempt. Hosts created by earlier releases only authorize the old provider-wide `keys/vps_ssh_key`; that file is no longer created but is still read as a fallback, so such hosts stay reachable from the machine that created them.

Bare placement no longer mints a separate agent client key: the host's VPS-root key (authorized on root at first boot) is the agent key. Every host-key pin `mngr_vps` writes is attributed to the host's record in the pin store, and both the online and the offline (stopped-instance) destroy paths remove the host's per-host key directory and its pin records once the VPS is gone (when the cloud destroy fails, the material is kept, with a warning, so the surviving instance stays reachable and destroyable from this machine). A host this machine already knows (its record cached here, or the instance stopped) but holds no key for is still destroyed through the cloud API: the placement teardown on the VPS is skipped with a warning rather than aborting the destroy. A running VPS whose key is absent here is not listed here; destroy it from the device that holds its key. A failed create removes that material too once its VPS is destroyed, but keeps it (with a warning) when the cleanup destroy fails, so the leaked instance stays reachable and destroyable from this machine. The shared provisioning flow now also destroys the VPS it created when a later boot or bootstrap wait fails (previously that instance leaked, since the create-time cleanup never learned its id), unless `MNGR_KEEP_FAILED_HOSTS=1` is set; when that destroy fails too, the create raises `VpsInstanceLeakedError` (naming the instance, with the original failure chained as its context) and keeps the host's key material. The `README` documents the per-host layout contract.

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entry `mngr-per-client-host-keys.md` in this directory belongs to the same PR.

- `create_host` combines main's instance-shape recording with the branch's leak-aware provisioning cleanup (`VpsInstanceLeakedError`).

- A VPS whose host key is not on this machine yet (a host created on another device) is logged at info during discovery rather than as a read failure on every poll.

- A per-host key dir that cannot be removed when its host is destroyed is logged at trace, as documented, instead of being silently ignored.

- When a VPS create fails after the instance exists and the rollback destroy fails too, the reported `VpsInstanceLeakedError` now names the provisioning failure (cloud-init timeout, host-key mismatch, sshd never up) and chains it as its cause, instead of replacing it with only the leak message.

- The per-host key layout is published as `VPS_HOST_ACCESS_MANIFEST` (`primitives.py`), which the provider's own key dir resolves through (its per-host `known_hosts` link target is the container placement's store; bare placement links `vps_known_hosts` itself); the container key-file names (`CONTAINER_SSH_KEY_NAME`, `CONTAINER_HOST_KEY_NAME`, `CONTAINER_KNOWN_HOSTS_NAME`) moved from `docker_realizer.py` to `primitives.py` beside the VPS ones.

- `resolve_vps_root_keypair` returns a `VpsRootKeypair` (the key path, its public half, and whether the retired provider-wide key was used) instead of a tuple, and a provider instance reports opening a host with that legacy key once per process at info, naming the instance (`is_legacy_key_fallback_logged`), instead of a debug line on every call. `VpsHostKeyMissingError` carries help text saying the host was created on another device or before per-host keys existed, so it must be reached from the device that created it or have its record synced here.

- The README's manifest paragraph says the published layout is the container-placement one and that minds materializes container placement only; the `mngr-per-client-host-keys` entry's destroy claim is corrected: a host this machine already knows but holds no key for is still destroyed through the cloud API, while a running VPS whose key is absent here is not listed here and is destroyed from the device that holds its key.

## 2026-10-16

The vps provider (and the providers built on it) reports that it cannot re-read one host on its own (`supports_incremental_host_discovery` is false), since it reads every host in one batched pass, so the streaming discovery poller's new between-poll host re-reads skip it.

## 2026-10-13

The rsync upload to a VPS outer host now also retries the two shapes a peer that hung up mid-handshake prints, not just the shapes a fresh VPS's networking flap prints.

Deciding whether an rsync failure is connection-class was being done in two places with two different lists of messages: this project's own, and mngr's (which had none for its rsync/git transfers at all, the bug fixed under MIND-478). The shapes that mean the peer closed the connection outright now live once, in mngr's shared `is_closed_ssh_connection_stderr`, and this project's list keeps only what a fresh VPS adds on top of them. No previously retried failure stops being retried; `Connection closed by <address> port <port>` and rsync's own "connection unexpectedly closed (0 bytes received so far)" start being retried here too.

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved 4 tests that sat under `Project-level checks` to the groups the source gives them; created the `Hardcoded paths` group; moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-12

The cloud VPS providers' discovery (aws, gcp, azure, vultr, ovh) now reports each host's recorded size (`DiscoveredHost.size`: the instance shape's vCPUs, RAM, and root disk, as `mngr list` reports `host.resource`) from the host records it already reads, so the `mngr observe` stream carries it. A record that holds no shape and whose plan is not in the provider's legacy plan table carries none, as does a stopped instance that discovery knows only from its cloud tags.

## 2026-10-10

`mngr list` now reports the real size of every `mngr_vps` cloud host instead of the hardcoded 1 CPU / 1 GB placeholder:

- `create` asks the cloud for the created instance's shape (a new `VpsClientInterface.get_instance_shape`: vCPUs, RAM, and the root disk) right after the instance exists and records it on the host record (`VpsHostConfig.shape`), so `get_host_resources` answers from the record alone, for stopped hosts too. A describe that fails is logged and the host lists with an unknown size rather than failing the create.

- A host record written before shapes were recorded answers from a small per-provider table of the plans in use at the time (the provider default plus the sizes the Imbue Studio create form offers); a plan outside it lists the size as unknown (`VpsHostSizeUnknownError`, which the listing turns into an absent `host.resource`) rather than a made-up number.

- The three cloud providers with a configurable root disk share one knob, `root_disk_size_gb` on `OfflineCapableVpsProviderConfig` (default 30 GB). The former per-cloud names (`root_volume_size_gb` on AWS, `boot_disk_size_gb` on GCP, `os_disk_size_gb` on Azure) still work as deprecated aliases that fold into the unified field; a settings block that sets both to different values is rejected with `VpsConfigError`.

- The shared cloud release profile asserts that the size `mngr list` reports for the created host matches what the cloud itself says the shape is.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-10-02

- Comment-only: a reference to the docker provider's source path now points at its new home in `libs/mngr_docker`.

## 2026-09-30

Removed the 9 comment lines that had accumulated inside `libs/mngr_vps/imbue/mngr_vps/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.

Test-only: the local fake outer host in the host-store tests implements the new `HostFileReadInterface.read_file_tail_from_offset` primitive (added in libs/mngr for ranged remote event reads), so the fake can still be instantiated.

## 2026-09-29

- `runsc_tmpfs_start_args`'s docstring no longer names the gen-2 slice bake as a caller that brings its own `/tmp`: the bake now carries only `--tmpfs /run` and gets its capped `/tmp` from mngr_vps like every other runsc container.

`VpsProvider._compute_extra_start_args` now receives the VM's MemTotal (in KiB), the value the create already reads to size the container's /tmp tmpfs. That lets a provider derive `docker run` args from the VM's RAM without a second read; the imbue_cloud slice provider derives its container memory cap from it.

Every cloud VM the `mngr_vps` family provisions (AWS, GCP, Azure, Vultr, OVH) now gets the container-host hardening the gen-2 slice fleet learned (issue #976; spec `specs/vps-container-parity.md`):

- The agent container's memory is capped at the VM's RAM minus a 1 GiB reserve for the VM's own daemons, so a workspace at capacity can no longer starve the VM's sshd and dockerd and wedge the host. Host setup installs an every-boot `mngr-vps-container-memory.service` that re-applies the cap from `MemTotal` (a resized VM re-caps on its next boot); the create runs it once right after the container starts and fails if the cap did not apply. A VM too small to leave any room after the reserve is left uncapped.

- A container running under gVisor (`docker_runtime = "runsc"`) gets `/run` and `/tmp` mounted as tmpfs automatically. runsc with `--overlay2=none` leaves the container rootfs on a filesystem that refuses to hard-link a unix socket, which is how supervisord installs its control socket, so without the mounts a workspace never comes up. Mounts a caller already configured are not repeated.

- The VM sshd is configured through a `/etc/ssh/sshd_config.d/60-mngr.conf` drop-in (`MaxSessions 100`, `MaxStartups 100:30:200`, `PermitRootLogin prohibit-password`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, and `PerSourcePenalties no` where sshd supports it) instead of two lines appended to `sshd_config`. The file is compared whole, so a re-run restarts sshd only when its content changed.

- The Docker daemon rotates container logs (`50m` x 3), bounds its build cache (`1GB`), and the journal is capped at 512 MiB. The daemon.json is merged with jq so `runsc install`'s runtime entry survives, and docker is restarted only when it is already running and the canonical content changed, so a re-run on a live host never bounces the workspace container for nothing.

The imbue_cloud slice path is unchanged: the shared host setup never ran on slices, and the tmpfs and memory changes leave a slice container's `docker run` line byte-identical. An externally provisioned VPS (`MinimalVpsProvider`) is not capped or verified by the create itself; if its caller runs the shared host setup, as the imbue_cloud delegated gen-1 rebuild does, the VPS still receives the sshd drop-in, the Docker and journal bounds, and the reconciler unit, which caps the container on the VPS's next boot; and since that rebuild runs the container under runsc, it now gets the `/run` and `/tmp` tmpfs mounts too.

The shared sshd tuning applied to every VPS now also writes `PerSourcePenalties no` when the VM's sshd advertises the option (OpenSSH >= 9.8, so every Debian 13 default image). Left on, the penalties lock a source address out for minutes after a few unauthenticated connections, which mngr's own discovery and readiness probes, the imbue_cloud connector's shared egress addresses, and a reconnect burst from the user's laptop all produce. The opt-out is guarded separately from the session-cap block, so a host re-provisioned after the caps were written still receives it, and it is skipped on older sshds that would reject the keyword.

- The Docker Engine and containerd pins carry over to Debian 13 unchanged: the install script derives the apt suffix from the VM's `/etc/os-release`, and the Docker repository serves `5:29.6.2-1~debian.13~trixie` and `2.2.6-1~debian.13~trixie` for amd64 and arm64.

- First-boot host setup retries `apt-get update` (six attempts, ten seconds apart) before installing the base packages. On Azure's Debian 13 image cloud-init reached the host-setup commands before the image's apt mirror resolved, the single `apt-get update` failed, and the whole first boot failed with it (no root key copy, no sshd tuning, no ready marker), so `mngr create` timed out waiting for the host.

- A VPS whose first boot never becomes ready (the instance never turns active, never answers SSH, or never writes the ready marker) is now destroyed by the failed `mngr create` instead of being left running. The instance id was previously only known to the create path once every boot wait had succeeded, so a wait that gave up leaked the VM; `MNGR_KEEP_FAILED_HOSTS=1` still keeps it for debugging.

The host-setup step that bounds the docker daemon and caps the journal now renders the journald cap through the shared `build_cap_journald_command` snippet in mngr core (`providers/ssh_host_setup.py`), which the lima provider uses too; the resulting drop-in (`/etc/systemd/journald.conf.d/60-mngr.conf`, `SystemMaxUse=512M`) is unchanged.

Merges the gen-2 learnings series (issues #975, #976 and the stopped-container recovery follow-up) into one branch; see the `mngr-host-sizing-debian-13`, `mngr-cloud-provider-updates`, and `mngr-vps-stopped-container-recovery` entries for the details. In short: every cloud VM gets the container memory cap (VM RAM minus 1 GiB, re-applied at every boot), `/run` and `/tmp` on tmpfs under runsc, the `/etc/ssh/sshd_config.d/60-mngr.conf` drop-in (with `PerSourcePenalties no` on Debian 13's sshd), bounded Docker logs, build cache and journal, `--restart=unless-stopped` on every container, `apt-get update` retries on first boot, and a failed boot wait destroys the VM instead of leaking it. A container that died behind mngr's back lists its agents, shows `STOPPED`, and revives through `mngr start`.

- Merging onto current `main` added three things the series did not have. The container's `/tmp` tmpfs under runsc is now executable and capped at one eighth of the VM's RAM (512 MiB on a 4 GB VM, about 1 GiB on 8 GB, never below 256 MiB), so a runaway write there fails with "No space left on device" instead of OOM-killing the whole sandbox (issue #1402); the provider reads `MemTotal` before `docker run` to size it. The VM's own `/tmp` stays on its disk: host setup overrides Debian 13's RAM-backed `tmp.mount` with a bind of `/var/lib/mngr-tmp`, and build contexts and rsync partials are staged under docker's data root instead of `/tmp` on every outer. And an explicit `--memory` in `default_start_args` or `-s` is honored: only containers mngr sized from the VM carry the `com.imbue.mngr.memory-cap=vm` label the every-boot reconciler now filters on, so it no longer silently raises a caller's smaller cap.

A `mngr_vps` cloud host whose container died behind mngr's back (the memory cap's OOM kill, a `docker stop` on the VM, a VM reboot) is now recoverable, and usually recovers by itself:

- Every container runs with `--restart=unless-stopped` unless the configured or caller start args already set a restart policy (the default-workspace-template's cloud create templates already pass it, so their `docker run` lines are unchanged), so an OOM kill, a Docker daemon restart, or a VM reboot brings a previously running container back on its own, with sshd relaunched by the container entrypoint. An explicit `docker stop`, which is what `mngr stop --stop-host` and the idle stop issue, still leaves the container stopped until `mngr start`.

- `mngr list` shows a reachable VPS whose container is merely stopped as `STOPPED` on the agent row as well as the host row (it said `CRASHED` on the agent row, since the host record carries no stop reason for an out-of-band exit), and `mngr start <agent>@<host>` finds and revives the agent instead of failing with "0 agent(s)"; the listing fix itself is in `mngr` (`docker cp -L`).

- The provider release trip stops the container out of band and asserts the host lists as `STOPPED` with its agent and `mngr start` revives it, then SIGKILLs the container's init the way the OOM killer does and asserts the host is back to `RUNNING` and reachable with no mngr command.

## 2026-09-26

Outer-box docker builds now forward env-sourced BuildKit secrets: for every `--secret ...,env=NAME` build arg, NAME's value in the creating process's environment reaches the `docker build` on the outer through its environment, so such a secret works on a vultr, OVH, or slice build exactly as it does on a local one.

## 2026-09-25

The container-setup and host-setup notes that still described the retired lima slice path (the slice guest's lima `additionalDisk`, the `lima_slice` provisioning module as the consumer of the pinned docker stack, and the lima-2.2.0 framing of the docker-ce pin rationale) now describe the qemu slice VM and its baked guest image.

The pinned docker install script is private to `host_setup.py` again (`_PINNED_DOCKER_INSTALL_SCRIPT`): the guest-image bake that consumed the exported `PINNED_DOCKER_INSTALL_SCRIPT` was the deleted lima slice provisioning.

The fully rendered bookworm apt versions (`PINNED_DOCKER_APT_VERSION`, `PINNED_CONTAINERD_APT_VERSION`) are removed: their only consumer was the deleted gen-1 box prep. The `*_CORE` pins stay and every install renders the distro suffix from the target's own os-release.

## 2026-09-24

The VPS discovery tests now use the shared listing script's new output format, where activity times and tmux panes are listed for all agents at once. VPS listings behave as before.

## 2026-09-23

A command that addresses an agent as `AGENT@host-....PROVIDER` on a VPS provider (aws, gcp, azure, ovh, vultr) now finds the host exactly as discovery would, so a pinned `mngr start` of a stopped instance works as it does with a bare agent id. The lookup still reads every VPS, so it is no faster than discovery on these providers.

## 2026-09-22

# Containers resolve their outer host as `host.docker.internal`

`run_container` now creates every container with
`--add-host host.docker.internal:host-gateway` (`OUTER_HOST_ADD_HOST_ARGS`, built
from `imbue.mngr.primitives.OUTER_HOST_HOSTNAME_IN_CONTAINER`), so a service the
VPS binds on its docker bridge address is reachable from inside the container by
that name without an address known at create time. `mngr_latchkey` uses it to
point a VPS workspace at the VPS-resident gateway directly, replacing the
VPS->container reverse SSH tunnel. Containers created before this change are
unaffected (the mapping cannot be added to an existing container);
`mngr_latchkey` keeps the tunnel for those.

A comment in `container_setup.py` no longer names a locally-rsynced `system/vendor/mngr/` as the
example of an uncommitted edit the docker build context carries.

## 2026-09-21

Agent listings from this provider now report the branch an agent's work_dir is
actually on.

mngr broadened `AgentDetails.initial_branch`: it used to name only a branch mngr
created, and is now the branch the work_dir was placed on either way, including
one that already existed and was merely checked out. This provider builds
`AgentDetails` by hand from the agent's `data.json` in its bulk-listing path
rather than going through the shared builders, so without this change its online
listing would have kept reporting the old narrower value while its offline path
reported the new one -- the answer would have changed with host state.

It reads through the shared `read_checked_out_branch` helper, so the fallback for
records written before the underlying field existed is applied identically
everywhere rather than restated per provider.

VPS discovery, snapshot-helper provisioning, and offline host_dir uploads now run on `mngr_executor` instead of a raw `ConcurrencyGroupExecutor`, so each worker thread's gevent Hub is destroyed when its task finishes. Before, VPS discovery leaked one Hub (a pipe pair plus the poll's objects) per VPS on every poll of a long-running `mngr observe`, the same leak fixed for imbue_cloud. A new `test_prevent_raw_concurrency_group_executor` ratchet keeps new raw executors out.

## 2026-09-17

Test doubles that implement `OuterHostInterface.write_file` now default `is_atomic` to `True`, matching the interface: `write_file` is atomic by default on every host (the bytes are staged in a sibling temp file and renamed over the destination once complete). No behavior change in this project's own code.

## 2026-09-12

Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

Container setup under gVisor (`runsc`) for gen-2 slices, the parameterized gVisor install script renderer, and extra SSH config files for the container sshd (the CA trust).

Constituent entries: `new-fleet-base.md`, `new-fleet-runsc-prototype.md`, `new-fleet-phase-4-impl.md`, `mngr-variable-sizing.md`, `mngr-slice-fleet-gen2-phase-3.md`, `mngr-new-fleet-testing.md`, `mngr-ssh-authority-in-vault.md`, `mngr-mirror-upstream-artifacts.md`

Security fix (gen-2 small follow-ups): the outer-side btrfs snapshot helper (`snapshot_helper.sh`, a root systemd service on every VPS and slice VM) no longer follows symlinks the workspace container can plant in the shared read-write trigger directory (`/var/lib/mngr-snapshot`, a plain bind mount with no user namespace, so container root is VM root there). Previously `cat request.json`, the `result.json` idempotency re-read, the `[ -f ]` startup probe, and the `> result.json.tmp` staging write all followed symlinks, giving the container a VM-root arbitrary-file read and, through the fixed temp name, a VM-root file truncate-and-overwrite. The helper now opens both protocol files with `O_NOFOLLOW` and checks the open descriptor is a regular file (perl one-liners, since bash cannot express either), treats anything else like an absent request, and stages each result in a random-named `O_CREAT|O_EXCL` temp file in the same directory that is `rename(2)`d over `result.json` (which replaces a planted symlink instead of writing through it). Hosts pick the new script up on their next realize (create, start, restore); no protocol change.

Gen-2 small follow-ups: the snapshot helper symlink tests share one helper that plants the outside-the-trigger-directory sentinel their symlinks point at.

- The pinned gVisor install script is now rendered by `render_gvisor_binary_install_script(release_url)`, parameterized on the directory holding the release's per-arch binaries and `.sha512` files. `PINNED_GVISOR_BINARY_INSTALL_SCRIPT` is unchanged in behavior (it renders against gVisor's upstream release bucket, `PINNED_GVISOR_UPSTREAM_RELEASE_URL`); the imbue slice fleet renders the same script against its artifact mirror.

This branch carries the earlier slice-fleet gen-2 phase 1-4 and variable-sizing spec work as its base; the substantive changes to this project are described by the `mngr-slice-fleet-gen2-phase-*` and `mngr-variable-sizing` entries in this same PR. The variable-machine-sizing implementation itself (specs/slice-fleet, phase 1) does not change this project.

The pinned docker/containerd apt version cores (`PINNED_DOCKER_APT_VERSION_CORE`, `PINNED_CONTAINERD_APT_VERSION_CORE`) are now public, so the gen-2 box prep's image customize can derive the distro suffix from the guest's own os-release (landing the identical pinned engine on trixie guests) instead of hardcoding the bookworm string.

Container SSH setup accepts extra SSH config files (`RealizePlacementContext.extra_ssh_config_files`, threaded through `setup_container_ssh`), used by Imbue Cloud gen-2 slices to install the tier's SSH certificate-authority trust into the workspace container's sshd (imbue-ai/mngr-internal#850).

This branch carries the slice-fleet gen2 phase 1-4 work by merging `mngr/slice-fleet-gen2-phase-4` as the base for the upcoming variable-workspace-sizing spec; the substantive changes are described by that branch's own `mngr-slice-fleet-gen2-phase-*` entries in this same PR. No additional changes to this project yet.

`new-fleet-base` is the integration base for the in-progress slice-fleet generation-2 program (specs/slice-fleet-gen2 and specs/slice-fleet): the squash of the formerly stacked PRs #571, #573, #574, #581, #609, and #614. It is deployed only to dev canaries and must not be deployed or merged as-is; follow-up PRs stack on it and the whole program lands on `main` as one change.

For this project it carries only the now-public pinned docker / containerd apt version cores (`PINNED_DOCKER_APT_VERSION_CORE`, `PINNED_CONTAINERD_APT_VERSION_CORE`), which the gen-2 box prep's image customize combines with the guest's own os-release to pin the identical engine on trixie guests. See this directory's `mngr-slice-fleet-gen2-phase-3` entry.

`build_home_volume_symlink_command` is the pure builder of the in-container command `setup_container_ssh` runs to point the container home at the host volume's `home/` subdirectory, so other container-recreation paths (the gen-1 -> gen-2 slice-fleet cutover) can replay the same link; `HOST_VOLUME_HOME_PATH` names the link's in-container target (`/mngr-vol/home`).

The pinned gVisor download + sha512 verification is now the public `PINNED_GVISOR_BINARY_INSTALL_SCRIPT` (with `GVISOR_RUNSC_RUNTIME_ARGS` / `GVISOR_RUNSC_BINARY_PATH`), so the gen-2 slice guest image customization in `minds_admin` bakes the identical pinned binaries into the image; the live-host `runsc install` step is composed from it and is unchanged in behavior.

- `ensure_btrfs_subvolume_on_outer` joins the new subvolume to the host quota group (`HOST_QUOTA_QGROUP`, `1/0`) when the data filesystem has one (gen-2 slices); filesystems without quotas are unchanged.

## 2026-09-03

The rsync-over-SSH transport used to copy directories to and from a container outer (`build_ssh_transport_for_outer`) quoted the `known_hosts` path for the shell only. rsync preserves that quoting when it splits the `-e` string, but ssh then splits `UserKnownHostsFile` on whitespace itself -- it is documented as "one or more files ... separated by whitespace" -- so a host directory containing a space became several nonexistent paths and every upload or download failed with `Host key verification failed`. The path is now quoted for ssh's parser as well, via the same `quote_ssh_known_hosts_option_value` helper that `build_ssh_transport_command` in mngr uses.

## 2026-08-25

Comment-only terminology sweep: mngr-level prose speaks host/agent instead of the minds-level workspace vocabulary.

Host-record and persisted-agent-data writes on the VPS unified volume are now atomic (temp file + rename via the outer host's atomic write path), so a concurrent reader -- e.g. a discovery pass on another mngr client -- can never observe a torn (empty / partial) record. Host-record reads also distinguish "unreadable" from "absent": parse failures are retried, and after retries either warn-and-treat-as-missing (default) or raise `HostRecordUnreadableError` when the new off-by-default `strict_host_record_parsing` mngr config flag is enabled. Part of the robustness fixes from the 2026-07-14 partial workspace-destroy incident.

Merged the machine/workspace naming cleanup branch (`mngr/fix-machine-workspace-naming`, PR #436) into this branch and resolved the merge conflicts against main. The changes themselves are described in this project's `mngr-fix-machine-workspace-naming` changelog entry.

## 2026-08-16

VPS host SSH info in live listings (`mngr list --format json`) now carries the explicit `known_hosts_path` that `SSHInfo` gained in this release, and the human-facing `ssh` command includes `-o UserKnownHostsFile=<path> -o StrictHostKeyChecking=yes` when the pin file is known, matching the shared construction paths.

## 2026-08-15

Phase 2 of the user-controlled-keys plan: per-host agent client keys and store-routed pinning.

The agent connection's client keys become per-host for new hosts: the container realizer mints a per-host `container_ssh_key` and authorizes only it in the new container, and the bare realizer mints a per-host `vps_ssh_key` and appends it to root's authorized_keys. `agent_endpoint` now takes the host_id it resolves keys for, falling back to the legacy provider-wide pair for hosts created before per-host keys existed.

The provider-wide `vps_ssh_key` deliberately remains the VM-root management key (discovery must reach a VPS before knowing which host lives on it, and OVH's rebuild bootstrap installs it), but it never serves the agent connection for new hosts.

`vps_known_hosts` and `container_known_hosts` are now rendered from mngr core's new host-key pin store: every pin (create, resume rebind, realizers) carries the host_id, `remove_host_from_known_hosts` is store-aware, and destroying a host forgets its pin records alongside its per-host keypairs. The per-host key-dir helpers moved to mngr core, with `mngr_vps.primitives` re-exporting them.

## 2026-08-13

No behavior change. This provider and the aws, azure, gcp, ovh and vultr providers built on it still report an agent blocked on a tool-approval dialog (or a question put to the user) as `RUNNING` rather than `WAITING`, where local, docker, ssh and lima agents now report `WAITING`.

Its listing builds agent state from a batched script that stats marker files by fixed name and has no agent object to ask, so it cannot see a per-agent-type block signal. It now says so explicitly at the call site rather than by omission.

## 2026-08-11

Improved remote docker build failure diagnostics: the error now reports the command's exit code (or "exit code unknown" when the output stream died) and tails stdout and stderr separately, so one stream's noise (e.g. buildkit progress on stderr) can no longer push the other stream's error text out of the reported window.

- The outer-VM btrfs snapshot helper no longer touches the data volume before
  it is actually mounted: `handle_request` now defers (polls `mountpoint`
  every 5s with a periodic journal warning, never fails) until
  `MNGR_BTRFS_MOUNT_PATH` resolves to a mounted volume. Previously, on slice
  VMs -- where lima mounts the data disk at a highly variable point late in
  boot, well after the helper's `local-fs.target` ordering -- the helper's
  startup replay of a `request.json` left over from before a reboot ran
  pre-mount: its `mkdir -p` wrote a shadow `snapshots/` directory onto the
  root fs beneath the unmounted mountpoint (debris that defeated the
  workspace autostart trigger during the mngr-internal#266 follow-up testing,
  see default-workspace-template#381), and the request was failed -- then
  permanently retired by the result-id idempotency guard -- when it only
  needed to wait a few seconds for the mount. A deferred request is now
  serviced normally once the volume appears, within the inner requester's own
  result timeout.

- `prepare_btrfs_on_outer` now refuses (raises `VpsProvisioningError`) when
  the btrfs mount path is a symlink -- the pre-mounted slice layout -- but
  nothing is mounted at its target yet. Previously the slice detection was a
  pure presence gate, so running before the VM's data-disk provisioning
  finished would silently fall through to building a btrfs loop file on the
  VM's root disk: a wrong-but-working state that masks the real volume (and
  its content) from then on.

## 2026-08-10

A Docker image build whose working directory has been deleted now fails with an error that says so, instead of a bare `FileNotFoundError` raised from inside `posixpath`.

A relative build context (every `[create_templates.*]` block passes `"."`) resolves against the building process's working directory. When that directory has been deleted, `Path(".").exists()` still returns `True` -- the process holds an open file descriptor on the unlinked directory -- so the context survives the is-it-a-path filter and then dies inside `Path.resolve()`'s `os.getcwd()`, naming nothing the operator can act on. `build_image_on_outer_from_build_args` now checks for this up front and raises `MngrError` explaining what happened. The build is not salvageable at that point, since the context directory is genuinely gone; the change is to the diagnosis, not the outcome.

Reverted the absolute-path branch added in #217, which was inert. It was guarded on `os.path.isabs(raw_context)`, but no configuration in either repo has ever used an absolute build context, and its stated premise -- that `Path.resolve()` calls `os.getcwd()` even for an already-absolute path -- is false, so it changed nothing for any input. Its `os.path.normpath` was also strictly less correct than the `resolve()` it replaced: `normpath` collapses `..` lexically, which walks through a symlink to the wrong directory.

The check is scoped to relative contexts on purpose. An absolute context needs no working directory and builds fine without one.

## 2026-08-05

Fixed `mngr create` crashing with `FileNotFoundError: [Errno 2] No such file or directory` during the image-build step when the create process's own working directory had been deleted (for example, a temporary build/clone directory cleaned up while it was still the CWD). `build_image_on_outer_from_build_args` normalized the build-context path with `Path.resolve()`, which calls `os.getcwd()` even for an already-absolute path -- so a deleted CWD took down the whole create even though the build context itself was fine. An absolute context path is now normalized without touching the CWD.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-30

Bumped the pinned Docker Engine version from 29.5.1 to 29.6.2. Under lima 2.2.0-carved slice VMs, docker-ce 29.5.1 with current containerd.io fails every `docker run` with a corrupted shim bootstrap (`failed to create TTRPC connection: unsupported protocol: Yunix`) -- this failed all 36 production slice bakes on 2026-07-30 -- while 29.6.2 with the same containerd.io 2.2.6 is verified working on the dev and staging slice fleets. Slice guest images pre-baked with the old pin must be re-staged: delete the box's `~limahost/.cache/mngr-slice-base/debian-base.qcow2` and re-run `just prep-server <id>`.

Also pinned `containerd.io` (2.2.6) alongside the docker-ce pin in the shared host-setup install script -- the docker/containerd daemon-shim pairing is exactly the axis the lima-2.2.0 shim-bootstrap failure lived on, so containerd floating with the repo while docker-ce is pinned reintroduces silent drift between hosts staged at different times. The install script is now exported as `PINNED_DOCKER_INSTALL_SCRIPT` so the slice VM provisioning installs the identical pinned stack.

## 2026-07-29

mngr_vps: add `imbue.mngr_vps.leak_cleanup`, the shared, age-based reaper for leaked VPS test instances used by every VPS-family provider (AWS, GCP, Azure, Vultr). It scans a provider's `list_instances()` output, keeps the instances a per-provider `CreatedAtExtractor` reports as test-created and older than a max age, and destroys them through the narrow `VpsReaperClient` protocol (`list_instances` + `destroy_instance`). Production instances, which carry no test marker, are never matched.

Failures are surfaced rather than swallowed: a `list_instances` scan error propagates (a reaper that cannot scan must not report "nothing leaked"), and a non-404 destroy failure raises `VpsLeakCleanupError` after every instance has been attempted (one stuck instance does not block reaping the rest, but the run still fails). A 404 on destroy is treated as already-cleaned (a race with a test's own teardown). The same `find_old_test_instances` / `destroy_leaked_instances` primitives back both each provider's session-end leak detector and its standalone CI reaper, so the two paths can never drift.

No behavior change. The bare realizer's listing test now asserts through the host_dir the script resolves rather than an interpolated literal, following the listing scripts gaining fallback host_dir candidates -- bare passes none, so it settles on the single host_dir it was handed.

`VpsProvider.create_host_on_existing_vps` now composes an overridable `_compute_extra_start_args()` seam into the container's effective `docker run` args (after `default_start_args`, before caller-supplied `start_args`, so explicit caller flags still win under docker's last-one-wins semantics). The base provider adds nothing; the imbue_cloud slice provider uses it to inject its per-slice container memory cap.

## 2026-07-26

Comment-only sweep for the default-workspace-template root declutter: the clone-overlay comment in `container_setup.py` now points its example at `system/vendor/mngr/` (the vendored tree's decluttered location). No behavior change.

Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: the new optional `volume_home_path` config gives the unified host volume a `home/` subdirectory backing a configurable container home path (e.g. `/home/user`) with `host_dir` inside it, plain-text service logs honor the new `host_log_dir` config, and the clone-overlay comment follows the template declutter. Defaults unchanged.

New optional `volume_home_path` config: the unified host volume gains a `home/` subdirectory that a configurable container path (e.g. `/home/user`) symlinks onto, with `host_dir` living inside it -- so the whole home tree persists on the btrfs-backed volume. `host_dir` must sit strictly inside the configured path (validated). Plain-text service logs honor the new `host_log_dir` config. Defaults preserve today's host_dir-symlink layout.

## 2026-07-24

Updated the `_LocalFakeOuter` test double to match the `HostInterface.execute_stateful_command` signature, which gained an optional `on_output` streaming callback. No runtime behavior change.

## 2026-07-14

Agent listings from this provider now populate `AgentDetails.pid` (the agent's main process PID in the remote host's PID namespace), extracted from the same already-collected tmux/ps probe data.

## 2026-07-11

The forever-claude-template repo is being renamed to default-workspace-template (with the `fct`/`FCT` shorthand expanded to `default_workspace_template`/`DEFAULT_WORKSPACE_TEMPLATE` forms).

References in this project (comments, identifiers, docs) are mechanically updated by `scripts/rename_template_repo.py`.

## 2026-07-07

VPS streaming discovery now reports each running host's SSH endpoint (via the shared `collect_cached_host_ssh_infos` helper) so the discovery poller re-emits it as a `HOST_SSH_INFO` event. Previously only a full `mngr list` surfaced SSH info, so a consumer that tunnels to VPS hosts through the streaming discovery path (e.g. the minds system_interface forward) could be left unable to reach them.

## 2026-07-06

Updated the per-host-bounded discovery override to accept the new cross-poll read registry parameter (unused by this batch provider, which reads all hosts in one bounded pass). No behavioral change for this provider.

Updated the VPS provider (and its offline-capable subclass) for mngr's new per-provider discovery: it now implements the bounded `discover_hosts_and_agents_within_timeouts` discovery entry point. Because VPS discovery reads all host records and their live agents in one batched pass, individual host reads are not separately bounded -- the provider is still bounded by the provider-level discovery error timeout, and no host is marked UNKNOWN by this path.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

## 2026-06-28

The container realizer can now run a base image that is already present in the VPS's local Docker daemon, skipping both the build and the pull.

`RealizePlacementContext` gained an opt-in `allow_local_image` flag (off by default, so OVH/vultr/aws behavior is unchanged); when set and the image is already loaded locally, `realize_placement` runs it as-is. This backs the imbue_cloud slice "build once per box, `docker load` per slice" acceleration.

## 2026-06-23

SSH host keys are now unique per host. Every VPS-backed host (AWS, GCP, Azure, OVH, Vultr, and imbue_cloud slices) gets its own freshly-generated VPS/VM-root and container sshd host keypair at create time, stored under `<key_dir>/host_keys/<host_id>/`, instead of one host keypair shared across every host a provider instance created. This removes the risk of one host's key being reused to impersonate another. The per-host keys are removed when the host is destroyed.

`mngr create --format json` surfaces the host's baked sshd host public keys (VPS/VM-root and container) via a new `get_ssh_host_public_keys` provider method, so pool-bake tooling can persist and pin them instead of scanning the host after creation.

Existing hosts created before this change keep working: the offline pause/resume path falls back to the legacy provider-global host key when a host has no per-host key recorded.

## 2026-06-22

Fixed host lock reporting for VPS/docker/bare hosts: a host's lock status is now derived from a real flock held-probe rather than the lock file's presence. The lock file now persists after release, so the previous mtime-based check would have reported every previously-locked host as permanently locked.

## 2026-06-21

- The agent container's PID-1 entrypoint now self-heals sshd: on every
  (re)start it restarts sshd once mngr has provisioned a host key, so the
  container is reachable again after a VM reboot or `docker restart` without
  waiting for `mngr start`. The explicit sshd (re)start used during setup and
  after a container restart is now idempotent (a no-op when sshd is already up).

- Register the gVisor (runsc) runtime with `--overlay2=none` so a container's
  writable layer is written through to the persistent Docker overlay2 layer and
  survives a `docker stop`/`start` or host reboot. Previously runsc used its
  default per-sandbox overlay (`--overlay2=root:self`), which is recreated on
  every start, so the injected sshd host key, the `/mngr` host_dir symlink, and
  mngr's provisioning markers were silently lost on restart -- leaving the
  container unreachable until mngr re-provisioned it. Applies to every provider
  that installs runsc via the shared VPS host-setup (aws, vultr, ovh, gcp,
  azure, imbue_cloud).

- Removed the now-dead gVisor self-overlay filestore-collision recovery from
  `start_container` (the reap-and-retry path only existed for the `root:self`
  overlay that `--overlay2=none` eliminates); `start_container` is now a plain
  `docker start`.

## 2026-06-19

Updated the VPS build-arg migration hint to reference the AWS provider's single `default_ami_id` knob (the separate `default_ami_by_region` config field was removed).

Introduced a `HostRealizer` seam inside the VPS provider as the first step toward
running agents directly on a cloud VM (no Docker container). The provider now
selects a realizer from a new `isolation` config knob (`IsolationMode.CONTAINER`
| `NONE`); `CONTAINER` is the default and preserves the original behavior
exactly. All Docker-container placement logic (image build/pull, container run,
in-container sshd setup, btrfs volume + snapshot helper, container
stop/start/teardown, and `docker commit` snapshots) moved behind a
`DockerRealizer` that the provider's base methods delegate to. The agent SSH
endpoint, placement lifecycle, and snapshots are now realizer concerns, while the
machine (provisioning, boot, instance lifecycle, host record, discovery) stays
with the provider.

Host-record store resolution also moved behind the realizer
(`realizer.open_host_store(outer, host_id)`), so a non-Docker placement can
persist its host record without a Docker volume. The container realizer
resolves the per-host Docker volume exactly as before.

Added the `BareRealizer`: it places the agent directly on the VM's OS (no
Docker), reached at `vps_ip:22` as root with the same VPS keypair the provider
already uses for the outer. It installs the lightweight host packages and mngr
host_dir layout on the VM (the same setup the container gets, applied to the OS),
keeps the host record in a plain root-disk directory, and reports no snapshot
support. Machine stop/start/destroy stays the substrate's job, so the bare
placement lifecycle steps are no-ops.

Discovery and listing also moved behind the realizer: finding the host on a
VPS, reading its running state, and collecting the live agent listing are now
realizer methods (`find_host_record`, `read_live_listing`, `is_placement_running`,
`collect_listing_output`). The container realizer keeps the exact Docker probes
(`docker ps` label lookup, `docker inspect`, `docker exec`); the bare realizer
reads the record from the fixed store path and runs the listing script directly
on the VM. Behavior-preserving for Docker.

The `AGENT_TAG_FIELDS` constant (used by the AWS/Azure tag-mirror code) is now
public, matching its sibling `AGENT_TAG_PREFIX`, so it is no longer imported as
a private name across modules.

`VpsHostConfig.container_name`/`volume_name` are now nullable so a bare host
record (which has no container or Docker volume) is representable, and the
agent-sshd wait now targets the realizer's endpoint port (the container port
for Docker, port 22 for bare) instead of hard-coding the container port.

`isolation=NONE` now builds the `BareRealizer`. The idle-shutdown action is a
realizer concern: the container realizer signals the container's PID 1
(`kill -TERM 1`), while the bare realizer powers the VM off directly
(`shutdown -P now`) -- so on a self-stopping cloud substrate a bare placement
needs no host-side sentinel/systemd watcher. The host_dir's outer-filesystem
path is also realizer-driven now (the btrfs subvolume for container; the fixed
root-disk store for bare), so the offline host_dir sync targets the right path
in both shapes.

Bare placement is gated to providers with a machine stop/start lifecycle: a
provider without one rejects `isolation=NONE` at create time
(`BareIsolationNotSupportedError`) rather than strand a VM it cannot restart.
AWS, GCP, and Azure all enable it. AWS/GCP bare self-stops the instance via the
OS-shutdown behavior; Azure bare instead runs the ARM self-deallocate from its
idle `shutdown.sh` (an Azure OS shutdown does not halt compute billing), reusing
the same deallocate call the container watcher uses and keeping the
self-deallocate role assignment. No user-visible behavior change for existing
container hosts on any provider.

Renamed the package from `mngr_vps_docker` to `mngr_vps` (the distribution
`imbue-mngr-vps-docker` to `imbue-mngr-vps`), since Docker is now one of two
placement shapes rather than the whole package. The shape-agnostic classes
dropped "Docker" from their names: `VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `MinimalVpsDockerProvider` ->
`MinimalVpsProvider`, `OfflineCapableVpsDockerProvider` ->
`OfflineCapableVpsProvider`, `TagMirrorVpsDockerProvider` ->
`TagMirrorVpsProvider`, `VpsDockerHostRecord` -> `VpsHostRecord`,
`VpsDockerHostStore` -> `VpsHostStore`, and the error base `VpsDockerError` ->
`VpsError`. The genuinely Docker-specific `DockerRealizer` and the
`container_setup` helpers keep their names. Mechanical rename; no behavior
change.

A bare create also rejects container-only inputs up front (an image override, a
Dockerfile build, or docker run start-args) rather than silently ignoring them.

Bugfix (found by the new bare release tests): on resume, the aws/gcp/azure
`start_host` read the host record through the Docker volume (`docker volume
inspect`), which does not exist for a bare host, so `mngr start` failed. It now
resolves the store through the realizer (the fixed root-disk path for bare).

The VPS provider's host-record updates use the type-safe `model_copy_update` /
`to_update` idiom instead of `model_copy(update={...})`, so field names are
checked by the type system.

The shared ``OfflineCapableVpsProvider`` now owns the cloud stop/start lifecycle: ``stop_host`` pauses the whole instance (so a paused agent costs only disk) and ``start_host`` resumes it, doing the resumed record's on-volume write *and* its external-store mirror together in one place. Providers supply only the cloud-API hooks (``_pause_cloud_instance`` / ``_resume_cloud_instance``) and override ``_sync_host_dir_before_pause`` / the known_hosts rebind where their behavior differs.

Scrubbed leftover "VPS Docker" wording from the provider's user-facing strings
(the config field description, the host-created success log, the
discover_hosts_and_agents log span, and the mutable-tags error messages), since
the provider now supports both container and bare placements. Removed the
redundant `_host_dir_path_on_outer` forwarder in favor of calling the realizer's
`host_dir_path_on_outer` directly.

Lifted three structurally-duplicated subsystems out of the aws/gcp/azure backends into the shared `OfflineCapableVpsProvider`, with small per-provider hooks:

- The self-stopping idle watcher (in-container sentinel `shutdown.sh`, the host-side systemd `.path`/`.service` install, and the bare-placement shutdown script) is now shared. The systemd units use a single shared name (`mngr-idle-watcher`); providers customize the `.service` body via `_idle_watcher_service_unit` (AWS/GCP default to `shutdown -P now`; Azure overrides to its ARM self-deallocate) and prepare the outer via `_prepare_idle_watcher_outer` (Azure installs curl + its deallocate script). The bare shutdown action is `_write_bare_idle_shutdown_script` (default: the realizer's poweroff; Azure: ARM deallocate, since an Azure OS shutdown does not halt billing).

- The host_dir-to-bucket sync daemon (install of the oneshot `.service` + `.timer`, and the before-pause flush) is now shared under the `mngr-host-dir-sync` unit name, gated on `_is_host_dir_sync_enabled` (off by default, so GCP installs nothing). Providers supply the sync CLI install (`_host_dir_sync_install_command`), the per-host `.service` body (`_host_dir_sync_service_unit`), and the target URI (`_host_dir_sync_target_uri`).

- `_on_host_finalized` is now a shared best-effort step runner: each step's failure is logged at WARNING and the rest still run, preserving the prior non-fatal contract. Providers extend the step list via `_post_finalize_steps` (Azure prepends its self-deallocate role assignment).

No user-visible behavior change; the host-side systemd unit names changed from per-provider (`mngr-aws-idle-watcher` etc.) to the shared `mngr-idle-watcher` / `mngr-host-dir-sync`.

Snapshot support is now a structural fact rather than a method that raises.
`snapshot_placement` / `delete_snapshot_placement` moved off the base
`HostRealizer` into a narrow `SnapshotCapableRealizer` sub-interface that only
the container realizer implements; the bare realizer is a plain `HostRealizer`
with no snapshot bodies. The provider gates its public snapshot operations once
at its boundary: a snapshot request on a bare placement now fails up front with
`SnapshotsNotSupportedError` instead of reaching into the realizer mid-operation.
No behavior change for container hosts; a bare snapshot still fails with a clear
error (just earlier).

The placement "is running" predicate is now computed exactly one way. Previously
the container realizer derived running-state two ways -- a cheap `docker inspect`
probe (`is_placement_running`, used by `get_host` / `discover_hosts` without a
full listing read) versus parsing the listing script's `CONTAINER_STATE` -- which
could disagree. Both now route through a single `is_running_container_state`
rule, and the cheap probe reads `.State.Status` (the same string the listing
emits) instead of `.State.Running`. The cheap probe path is preserved: the
get-host / discover callers still learn is-running without triggering the heavy
listing script. No behavior change.

The realizer's placement-lifecycle methods now take an opaque `PlacementHandle`
(the container name/id/volume bundle for the container realizer; empty for bare)
instead of the whole `VpsHostRecord`. The realizer mints the handle in
`realize_placement` and the provider extracts it once from the persisted record
at each call boundary (`PlacementHandle.from_record`), so the repeated
`record.config.container_name` reads (and their `assert record.config is not None
and record.config.container_name is not None` guards) collapse to a single
boundary extraction, the bare realizer's "ignore the record" becomes an explicit
empty handle, and `start_activity_watcher` no longer takes a `container_name`
parameter (the realizer reads it from its handle). Internal refactor; no
user-visible behavior change.

Moved the pure VPS build-arg parsing helpers (`ParsedVpsBuildOptions`, `extract_single_value_arg`, `extract_git_depth`, `extract_presence_flag`, `parse_vps_build_args`, `raise_if_vps_migration_arg`, `raise_if_unknown_provider_arg`) out of `instance.py` into a new `imbue.mngr_vps.build_args` module. Mechanical extraction; no behavior change.

Moved the offline / tag-mirror provider subsystem (`OfflineCapableVpsProvider`, `TagMirrorVpsProvider`, and their idle-watcher / host_dir-sync unit builders and agent-tag constants) out of `instance.py` into a new `imbue.mngr_vps.instance_offline` module. `instance_offline` imports `VpsProvider` from `instance`; the dependency is one-directional. Mechanical extraction; no behavior change.

Extracted the offline read-side reconstruction shared by the tag-mirror (AWS/Azure) and GCE-metadata (GCP) providers into a new `KeyValueMirrorVpsProvider` base in `instance_offline`, sitting between `OfflineCapableVpsProvider` and `TagMirrorVpsProvider` (which now extends it). The base owns reconstruction over a `dict[str, str]` key-value mirror: reassembling per-agent records, computing the per-field upsert/delete set (`_agent_field_items`), building STOPPED discovered/offline hosts, parsing the created-at value, and resolving instances by host id. Providers supply the map (`_offline_kv_map`), the optional per-value length cap (`_max_value_len`, 256 for tags and uncapped for metadata), and the host-name key (`_host_name_key`, renamed from the old `_host_name_tag_key`). The bucket-or-tags routing and the external `_state_store` stay on `TagMirrorVpsProvider`; GCP inherits no bucket machinery, and the per-provider agent-record write side is unchanged. Internal refactor; no user-visible behavior change.

Hardened systemd unit generation. A new `imbue.mngr_vps.systemd.render_systemd_unit` helper centralizes the unit-file format so the offline-capable provider's `.path` / `.service` / `.timer` units are no longer hand-assembled as `[Section]\nKey=Value\n` strings. The idle-watcher poweroff action moved from an inline `ExecStart=/bin/sh -c 'rm -f <sentinel> && shutdown -P now'` to an installed `/usr/local/sbin/mngr-idle-watcher.sh` script (written by the default `_prepare_idle_watcher_outer`), matching the existing Azure self-deallocate script pattern, so the sentinel path no longer has to survive systemd's plus the shell's nested quoting. The host_dir-sync command is likewise installed at `/usr/local/sbin/mngr-host-dir-sync.sh` (see `build_host_dir_sync_script`) and referenced by `ExecStart` directly. No behavior change.

Deduplicated the cloud state-bucket / offline-read-volume layer. A new
`BaseObjectStoreVolume` in `state_bucket_base.py` implements the six `Volume`
methods (`listdir`, `path_exists`, `read_file`, `remove_file`,
`remove_directory`, `write_files`) plus the `_as_dir_prefix` normalization once,
in terms of a small set of per-cloud SDK primitives (`_iter_delimited_entries`,
`_prefix_has_any_object`, `_has_object_at_key`, `_read_object_bytes`,
`_delete_single_object`, `_delete_prefix`, `_write_object`) and a normalized
`ObjectStoreEntry` listing shape; `S3Volume` / `BlobVolume` become thin
subclasses. A shared `_ObjectStoreErrorSeam` (`_translate_errors` /
`_is_not_found` / `_bucket_error_type`) lets the base run each SDK op inside the
cloud's error translation and special-case not-found, and `_get_object` /
`_delete_object` / `_prefix_has_objects` now live on `BaseStateBucket` in terms
of that seam. Internal refactor; no user-visible behavior change (the only edge
the two clouds previously disagreed on -- `path_exists("")` on an unscoped raw
volume, never reached in practice -- is now uniformly `False`).

Added a shared `cli_helpers` module for the cloud providers' operator CLIs: `resolve_provider_config` (the `[providers.<name>]` lookup + wrong-backend warning + class-defaults fallback) and `refuse_if_managed_resources_exist` (the cleanup-refusal guard that blocks deleting shared network infrastructure while mngr-managed instances still exist). The AWS / Azure / GCP / OVH CLIs now delegate to these instead of carrying near-identical copies. The refusal now raises a single `ManagedResourcesExistError` (a `VpsError` / `MngrError`) across all providers, so `mngr <cloud> cleanup` renders the "refusing to clean up" message identically whichever cloud you are on. Added config bases `OfflineCapableVpsProviderConfig` (carries `allowed_ssh_cidrs`, shared by AWS/Azure/GCP) and `PublicIpVpsProviderConfig` (adds `associate_public_ip`, shared by AWS/Azure only -- GCP names its equivalent field `associate_external_ip`, so it extends the former directly), with `allowed_ssh_cidrs` unified to `ScalarStrTuple`. No user-facing config key changed.

Lifted more cross-provider glue out of the aws/azure/gcp backends into the shared offline layer (`instance_offline.py`), with small per-provider hooks. The near-identical EC2/VM tag-mirror `HostStateStore` is now a single `TagHostStateStore` typed over `TagMirrorVpsProvider`, with the only cloud-specific step (the tag-removal API call) behind a new `_remove_instance_tags` hook (and `_persist_agent_to_tags` now declared abstract on the base). The object-storage providers' shared host_dir-sync install (the systemd `.service`/`.timer` write sequence) moved up into `BucketHostDirBackend.install_sync`; each provider supplies a `HostDirSyncInstallPlan` (install command, sync command, `.service` body, target URI) or `None` to skip, plus a `_cloud_label` hook. The `_state_store` selection (bucket when present, else the tag store) is now concrete on `TagMirrorVpsProvider`, driven by new `_state_bucket` / `_bucket_error_type` / `_bucket_label` hooks; the cloud-specific bucket-existence probe stays per-provider. `_list_provider_vps_hostnames` (cached listing -> non-empty `main_ip`) is now the default on `KeyValueMirrorVpsProvider`. A new `VpsProvider._require_parsed` helper replaces each provider's hand-written `match`/type-narrowing guard in `_create_vps_instance`. No user-visible behavior change.

Localized internal cleanups (no user-visible behavior change): a shared `_run_provisioning_step` helper collapses the repeated "run an outer command, raise `VpsProvisioningError` with its stderr on failure" shape across the btrfs/dir/systemd provisioning steps in `container_setup`; the four structurally-identical `teardown_placement` cleanup blocks in `docker_realizer` route through a single `_record_cleanup_attempt` helper; a `VpsHostRecord.with_certified_updates` method wraps the "update the nested certified data, then re-wrap the record" idiom at its three call sites; a `VpsProvider._write_and_mirror` method pairs every on-volume `write_host_record` with its external-store mirror in one place (the two had a history of drifting apart); a `VpsProvider._write_shutdown_script` method shares the `commands/shutdown.sh` mkdir/write/chmod plumbing across the container/bare/sentinel/deallocate variants; and the duplicated base64 remote-script wrapper (`_remote_sh_command`) is gone in favor of the shared `host_setup.build_remote_script_command` (renamed from the private `_remote_script_command`).

Bugfix (found by the GCP bare release test): on resume, the shared
`OfflineCapableVpsProvider.start_host` waited only for *any* sshd handshake
(`wait_for_sshd`) before its strict-host-key-checked connect, but never waited for
the VM to actually serve mngr's expected host key the way create does. On GCP the
GCE startup-script re-runs on every boot and `systemctl restart ssh`s partway
through, so the any-key wait could return inside that restart window and the
strict connect then hit a refused/mismatched port 22 -- failing `mngr start` with
"Unable to connect to port 22". This surfaced for bare placement specifically,
whose agent endpoint *is* port 22 (containers reach the agent on a separate,
stable container port). `start_host` now mirrors create's host-key wait
(`_wait_for_expected_host_key` on port 22, using mngr's VPS host public key) right
after the sshd wait, in the shared base. Cloud-init backends (AWS/Azure) inherit
the no-op default and return on the first poll; GCP's existing override polls
until its startup-script-installed key is live, riding out the ssh restart.

Integrated the `mngr/volumes` offline-store simplification (commit `f8bb5c0a5`): the per-agent instance-tag mirror is removed in favor of a single uniform external `HostStateStore` per provider -- AWS/Azure use their object-storage state bucket as the sole offline store (a stopped host's offline metadata now requires the bucket; the provider's `_state_store` raises an actionable `missing_state_bucket_error` pointing at `mngr <cloud> prepare` when the bucket is absent), and GCP uses a lossless instance-metadata-backed store (full host record + one JSON value per agent). AWS/Azure/GCP now extend `OfflineCapableVpsProvider` directly. This supersedes the earlier-on-this-branch tag-mirror dedup (the lifted `TagHostStateStore` / `KeyValueMirrorVpsProvider` / `TagMirrorVpsProvider` are gone); the realizer architecture, the systemd-unit hardening, and the cli/config/state-bucket dedup are retained. No behavior change for container hosts beyond the offline-metadata-requires-bucket consequence noted above.

Bugfix: a running bare (`isolation=NONE`) host is now discoverable and reachable
with the default provider config -- `mngr conn <agent>` (and every other
operation: list, stop, start, destroy, snapshot, label, agent persistence) no
longer requires re-specifying `-S providers.<cloud>.isolation=NONE` at connect
time. The provider previously built a single realizer from `config.isolation` and
used it for ALL operations, so a bare host probed by the default container
realizer found no container and was invisible/unreachable (it was reached on the
container port 2222 instead of the VM's port 22). Now `config.isolation` selects
the realizer only for newly-created hosts; operations on an existing host resolve
that host's own placement -- from the host record (`container_name is None` means
bare) once it is read, and, in discovery (which has only the VPS IP, before any
on-host store is opened), from a new `mngr-isolation` instance marker (an EC2/
Azure tag, a GCE metadata item) stamped at create and readable from the cloud API
without SSH. A host created before this change carries no marker and defaults to
container, preserving the prior behavior.

Behavior-preserving dedup in the shared offline layer. The near-identical AWS/Azure bucket-store selection now lives on `OfflineCapableVpsProvider` as `_select_bucket_store` (builds a `BucketHostStateStore`, or raises the actionable `missing_state_bucket_error` when the bucket is absent) and `_select_bucket_host_dir_backend` (bucket-backed when the feature is enabled and the bucket is present, else the no-op `NullHostDirBackend`); the AWS/Azure `_state_store` / `_host_dir_backend` cached properties are now thin wrappers passing only their resolved bucket, label, and prepare-command. The shared offline discovery now has a concrete `_offline_discovered_host_from_instance` default (builds a STOPPED `DiscoveredHost` from the `mngr-host-id` / name identity tags) with a `_host_name_tag_key()` hook; AWS/Azure drop their copies and set only the key (`Name` / `mngr-host-name`). The resume known_hosts rebind's add half is now a shared `_add_known_hosts_for_ip` helper (adds the VPS port-22 and container endpoints, each only when its key is present). GCP is unaffected: it keeps its metadata-backed `_state_store`, the `NullHostDirBackend` default, and its metadata-encoded discovery override. No user-visible behavior change.

Hardened the post-finalize idle-watcher install. A missing host record at
`_on_host_finalized` -- which runs only after the record has been made durable, so
a missing record there is a broken invariant rather than a tolerable condition --
now fails `create_host` (raising `HostCreationError`, whose cleanup tears the VPS
back down) instead of logging a WARNING and silently shipping a host that can never
auto-stop on idle. Genuine idle-watcher *install* failures (a network or systemctl
error) stay best-effort and tolerated as before. Also documented why the remaining
warn-not-raise sites in the provider (snapshot-before-stop, the activity-watcher
relaunch on resume, the agent-record id guard, and the malformed-identity skip in
offline discovery) warn rather than raise, cross-referencing their Modal/Docker and
online-discovery equivalents.

Added a shared `_remirror_host_name` hook on `VpsProvider`, called from the base
`rename_host` after the record write. Offline discovery recovers a stopped host's
name from a cheap instance tag/metadata stamped at create (not from the mirrored
record), so without re-stamping it a renamed host kept listing under its old name
once stopped. The base hook is a no-op (providers with no such identity tag need
nothing); the offline-capable cloud providers (AWS/Azure/GCP) override it to update
the identity through their cloud API. The hook runs whether the host is up or
stopped, since the cloud-API tag/metadata write does not require a reachable host.

Performance: `mngr stop` on a bare (`isolation=NONE`) host no longer hangs for minutes while it captures the host's `host_dir`. A bare `host_dir` holds the agent's full working tree (a git checkout is thousands of small files), and the offline capture writes one object per file to the state bucket; these uploads previously ran serially, so one wide-area round-trip per object made the capture take minutes (the EC2 pause only happens after the capture completes). The per-file uploads now run concurrently across a bounded worker pool, turning a minutes-long stop into seconds. Found by a real-cloud smoke test; container hosts were unaffected (their `host_dir` is small).

Internal cleanup (no behavior change): dropped the unused `sentinel_on_outer`
parameter from the `_idle_watcher_service_unit` hook -- the sentinel removal lives
in the poweroff/deallocate scripts, not the oneshot `.service` body.

Bugfix: `mngr snapshot delete` on a VPS now actually takes effect. `delete_snapshot`
previously removed the docker image but never dropped the snapshot from the host
record, so `mngr snapshot list` kept showing a deleted snapshot (and an unknown id
succeeded silently). It now removes the entry from the record (raising
`SnapshotNotFoundError` for an unknown id) and refreshes the cache. The image
removal (`delete_snapshot_placement`) now treats an already-absent image as success
but raises on any other failure -- so a failed delete is no longer reported as one,
and the snapshot stays listed until its image is really gone. (The docker provider
still only warns here; the VPS provider deliberately raises.)

`isolation_from_marker` now parses a *present* `mngr-isolation` marker strictly: an
unrecognized value raises rather than silently resolving to CONTAINER (an absent
marker still defaults to CONTAINER for pre-marker hosts, since the marker is
mngr-written and a bad value is corruption worth surfacing).

Extracted `host_name_from_prefixed_value` (shared by `host_name_from_tags` and GCP's
metadata-based recovery) so the strip-`mngr-`-prefix / host-id fallback logic lives
in one place. No behavior change.

Trimmed the README to user-relevant content and tightened it for concision.

Aligned the base config field descriptions (surfaced via `mngr config`/help) with the README's wording, including correcting the `container_ssh_port` description that wrongly claimed localhost-only mapping.

Fact-checked the README against the `mngr_vps` module and documented the `isolation` mode (container vs bare placement), the bare-host lifecycle, and the realizer architecture.

Added `VpsCloudReleaseProfile` and `find_handle_by_launched_label` to `testing.py`: shared plumbing for the VPS-family cloud providers (AWS/GCP/Azure) in the new provider release-test harness. It implements the cost-stop, sketchy-kill, and backend-clean probes through the common `VpsClientInterface`, so each cloud provider's release trip reduces to declaring its credential gate, settings.toml, and pytest-launched label. It also declares `snapshot_survives_destroy = False` for Trip 3 (the container shape's docker-commit snapshot lives on the VPS disk and is not portable).

Extended `VpsCloudReleaseProfile` for Trip 4 (error classification): it declares that the VPS-family clouds raise the contract `ProviderUnavailableError` on unresolvable credentials and reject `--vps-*` build args via the shared migration check; per-provider subclasses supply the credential-unresolvable env and whether the missing-credential help text is curated.

Extended `VpsCloudReleaseProfile` for Trip 2 (idle auto-shutdown): it declares the cloud trio resumes after auto-shutdown and drives the shutdown via the idle watcher (`mngr create --idle-timeout`), whose poweroff lands the VM in its resumable stopped state (AWS stop / GCP TERMINATED / Azure deallocated).

`VpsCloudReleaseProfile` now sets the harness's `is_bare_host` flag from its `IsolationMode` (NONE -> bare), so Trip 1 runs its bare-shape assertion for the NONE parametrization -- the coverage the retired per-provider bare lifecycle tests used to own.

Agent lifecycle detection now targets the agent's primary tmux window by name (the configurable `tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index` setting.

Added a uniform offline host/agent-state store for the cloud providers. A new `HostStateStore` abstraction (`host_state_store.py`) is the single interface every offline-capable provider uses to mirror the authoritative on-volume host record + per-agent records to an external store for offline reads (persist/delete the host record, persist/remove/list agent records, read the host record). The base `VpsDockerProvider` calls new `_persist_host_record_externally` / `_delete_host_record_externally` hooks after every on-volume host-record write (create, stop, snapshot, rename, certified-data sync) and on destroy; `OfflineCapableVpsDockerProvider` owns the shared persist/remove envelope and the offline discovery/listing path, delegating the per-provider step to `_mirror_agent_record` / `_remove_mirrored_agent_record` / `_offline_agent_dicts_for`. Providers that do not opt in (Vultr, OVH, imbue_cloud) are unaffected.

The object-storage implementation `BucketHostStateStore` (over a `StateBucket` protocol, shared by AWS S3 and Azure Blob) treats the bucket as required infrastructure: a provider whose bucket has not been provisioned raises an actionable error (pointing at its `prepare` command) the moment its state store is accessed -- on the create/label write path as well as on offline reads. Storage errors propagate rather than being swallowed (a dropped write would let a stopped host show stale state; a swallowed read would make it vanish from `mngr list`), and a malformed record raises rather than being silently dropped.

Added the offline `host_dir` capability as a select-once `HostDirBackend` strategy (the sibling of the state store): a cloud-agnostic, bucket-backed `BucketHostDirBackend` (capture + offline-read volume) or a no-op `NullHostDirBackend` when the feature is off. Capture is **operator-driven** -- `capture(host_id, vps_ip)` rsyncs the host's `host_dir` off the box (via the operator's outer SSH connection, skipping sockets/other special files natively) into a local temp dir and uploads it to the bucket with the operator's own credentials at `mngr stop` -- so there is no instance/managed identity and no on-box sync daemon. It is best-effort (a capture failure must not break `mngr stop`); the read path returns no volume for an empty `host_dir` prefix (nothing captured yet) and propagates only a bucket probe error.

Added supporting shared modules: `state_keys` (the `hosts/<id>/host_state.json`, `agents/`, `host_dir/` object-key layout plus the `managed-by` tag constants), `BaseStateBucket` (cloud-agnostic record marshalling, key layout, and offline-read volume, so each cloud's bucket implements only raw object primitives plus its SDK client and bucket lifecycle), and `normalized_tags_to_dict` / `host_name_from_tags` for reading a stopped instance's cheap `mngr-*` identity tags during discovery.

Fixed a VM-leak bug: `mngr destroy` of a stopped (deallocated / powered-off) host no longer leaves the underlying cloud instance and its mirrored state behind. The base `VpsDockerProvider.destroy` tears the host down over SSH using its host record, which works only while the instance is reachable; a stopped instance (whose disk persists but whose OS is down) either made destroy raise `HostNotFoundError` and leak the still-billing instance, or -- with a stale cached record from an in-process `mngr stop` -- run a doomed SSH teardown against a dead address and still leak. `OfflineCapableVpsDockerProvider` now overrides destroy to dispatch up front on the instance's own power state (resolved from its `mngr-host-id` tag/label, no SSH): a stopped instance goes straight to an offline teardown that terminates it through the same cloud `destroy_instance` primitive the online path uses, cleans up the per-host provider SSH key, and deletes the external state (host + agent records). A running or unresolvable instance delegates to the base path. It fails loudly -- a termination that could not be carried out raises a `CleanupFailedGroup` rather than reporting success, so a leaked instance can never masquerade as a clean destroy -- while a genuinely already-gone instance is treated as idempotent success.

## 2026-06-17

Added `OfflineCapableVpsDockerProvider`, a base for cloud providers (AWS/GCP/Azure) whose hosts can be stopped while their disk persists. It consolidates the previously-duplicated offline discovery and host resolution -- reconstructing stopped (SSH-unreachable) hosts and their agents from the provider's instance listing, and falling back to that listing when the on-volume path is unreadable -- behind a small set of per-provider hooks.

The same base now also owns the shared stop/start lifecycle (idle-pause + resume), instance lookup by host-id, SSH known_hosts rebinding, and the self-stopping idle watcher install, with the cloud-specific bits (pause/resume the instance, the idle action, Azure's static-IP/self-deallocate variations) supplied through hooks. No user-visible behavior change.

## 2026-06-16

Fix `start_host` (the `mngr stop --stop-host` resume path) to restart the container's sshd after `docker start`. sshd is launched via `docker exec`, not the container entrypoint, so it does not survive a container stop/start (or a host VM reboot that takes the container down, e.g. an AWS instance stop/start) -- without restarting it, the resume timed out waiting for container SSH. This was a latent gap for every VPS-Docker provider; AWS's native instance stop/start surfaced it.

`start_host` now also relaunches the in-container activity watcher on resume and records a fresh `BOOT` activity timestamp. The watcher is a backgrounded process that does not survive a container stop/start, so without relaunching it a resumed host would silently stop auto-stopping on idle (a latent gap for every VPS-Docker provider). Refreshing `BOOT` activity is required alongside the relaunch: otherwise a resumed-but-idle host keeps its pre-stop activity-file mtimes, and the watcher re-stops it within one poll -- so resuming an idle host would race a near-immediate auto-stop.

## Stopped containers no longer misreport as CRASHED / vanish from `mngr conn`

- Fixed: a VPS-Docker host whose container is stopped while the VPS itself is still reachable (the idle-watcher shutdown, a manual `mngr stop`, or a VPS reboot) is now reported as `STOPPED` and stays visible to `mngr conn` / `mngr start`, instead of being misreported as `CRASHED` and filtered out of the connect path entirely (which produced a confusing "Could not find agent" even though the agent had stopped cleanly and its data was intact).

  Discovery now distinguishes a reachable-but-container-down host (clean stop) from an unreachable VPS (the genuine down/crash case). The latter is unchanged: it stays hidden from `include_destroyed=False` callers and surfaces as `CRASHED` in `mngr list`. This affects all VPS-Docker providers (aws/gcp/azure/vultr/ovh).

- Fixed: `start_host` now re-starts sshd inside the container after restarting it, so `mngr start` (and `mngr conn`'s auto-start) actually recovers a stopped agent. The container's sshd is launched via `docker exec`, not its entrypoint (`tail -f /dev/null`), so a `docker start` brought the container back *without* sshd; `start_host` then waited for an sshd that never came up and timed out, leaving the agent unrecoverable (the user hit this after an idle-stop). It now calls `start_container_sshd` between the container start and the SSH wait. `docker start` is a no-op on an already-running container, so this also repairs a container-up-but-sshd-down host. This affects all VPS-Docker providers; it is exercised by the existing `test_provider_lifecycle_create_stop_start_destroy` release test (which create → stop → start → exec).

Removed the dead disk-snapshot and SSH-key-listing surface from the VPS client layer. `VpsClientInterface` no longer declares the `create_snapshot`, `delete_snapshot`, `list_snapshots`, or `list_ssh_keys` abstract methods -- these had no production callers (provider-level host snapshots go through `docker commit`, a separate path). `ExternallyManagedVpsClient` correspondingly drops its stub overrides for those operations, and the now-unused `VpsSnapshotInfo` / `VpsSshKeyInfo` models and the `VpsSnapshotId` primitive were deleted. The remaining mandatory client surface is the instance lifecycle plus `upload_ssh_key` / `delete_ssh_key`.


The `mngr_vps_docker` README's module list no longer claims `VpsClientInterface` provides snapshot operations.

`destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign (no failure); a resource that exists but could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a host-record write failure), so `mngr destroy`/`cleanup` can surface it and exit with an informative, cause-specific code.

Benign "already gone" is detected by signal rather than fragile error-text matching: the VPS API steps (instance destroy, SSH-key delete) classify by the `VpsApiError` HTTP status code (404/410), and `remove_container` gained a `tolerate_missing` option so an already-absent container is a no-op (matching `docker volume rm -f` semantics). See `specs/cleanup-error-aggregation.md`.

- **Host discovery no longer aborts `mngr create` on a transient SSH keypair race.** Discovery SSHes every VPS the provider enumerates (and the OVH backend lists every VPS in the account), each lazily creating the local SSH keypair on first use. A racing, half-written `.pub` made paramiko's certificate probe raise a bare `ValueError` that escaped `_read_records_from_vps` (which only catches `MngrError`) and failed the whole sweep. The root cause is fixed in the shared `mngr` layer (race-free keypair creation plus wrapping that probe error as a structured `HostConnectionError`), so the existing best-effort `MngrError` handling now degrades a bad VPS to a warning rather than aborting create.

## 2026-06-15

The `host_backup` btrfs snapshot helper (`snapshot_helper.sh`, the `OUTER_TRIGGER` mechanism) no longer re-processes a request it has already serviced. The helper never consumes `request.json`, and both its startup path and (formerly) a missing-`inotifywait` crash-loop could re-run the last request; re-running a snapshot whose target path now exists overwrote a good `result.json` with a spurious "snapshot path already exists" failure, masking the successful backup. The helper now skips any request whose `request_id` already appears in `result.json`. request_ids are unique per request, so this never suppresses a real new request, and a genuinely-unserviced request still runs via the startup path.

Clarified the `provision_snapshot_helper_on_outer` docstring: it still assumes `inotify-tools` and `jq` are pre-installed on the outer, and now notes the slice path installs them in its lima VM provisioning (in addition to the cloud-init and SSH host-setup paths).

`prepare_btrfs_on_outer` now detects when the btrfs filesystem is already mounted at the configured mount path (and there is no loop image) and skips the loopback allocation/format/mount/fstab steps, just ensuring the per-host subvolume. This lets a host whose btrfs is provided by an already-mounted disk (an OVH bare-metal "slice" VM's lima data disk) reuse the shared vps_docker bake and slow-path rebuild unchanged -- no loopback image is created over the real mount.

## host-setup: OS-aware Docker install

- The pinned Docker install step derives the apt repo (`download.docker.com/linux/$ID`) and the full apt version suffix (`~$ID.$VERSION_ID~$VERSION_CODENAME`) from `/etc/os-release` at run time rather than hardcoding the Debian 12 / bookworm strings, so it is distro-aware across the Debian family. On the Debian 12 default it renders the same apt version (`5:29.5.1-1~debian.12~bookworm`) and repo URL (`linux/debian`) every backend already used; the derivation additionally covers a non-default `--gcp-image` (e.g. an Ubuntu LTS image) without a code change. `PINNED_DOCKER_APT_VERSION` is exported as the fully-rendered Debian 12 apt version string for any caller or test that needs the exact value rather than the runtime-derived suffix.

## bootstrap: direct root-key injection

- The first-boot bootstrap (cloud-init `user-data` and the GCE `startup-script`) writes the provider SSH public key straight into root's `authorized_keys`, independent of the copy-from-default-user (`admin` / `ec2-user` / `ubuntu` / `debian` / ...) step, via an `authorized_user_public_key` parameter that `_provision_vps` always passes. This removes any reliance on a cloud image's default-user key landing in root. It is the deciding fix for GCE, where the google guest agent provisions the `ssh-keys` metadata into the `ubuntu` user asynchronously and races the default-user copy, intermittently leaving root without the key. Additive and idempotent for the AWS / Vultr / OVH backends (the key also lands in root via the default-user copy, so the extra line is a no-op duplicate).

## bootstrap: backend override hooks for non-cloud-init images

- `VpsDockerProvider` gains two override hooks: `_generate_bootstrap_payload` (default cloud-init `user-data`; a backend whose images run the google-guest-agent instead of cloud-init, e.g. GCP, overrides it to render a `startup-script`) and `_wait_for_expected_host_key` (default no-op; overridden when the host key is installed post-boot, to wait for it before strict-checking). Provisioning is otherwise backend-agnostic -- both payloads render the same shared `host_setup.build_host_setup_steps` and write the same marker.

- The `mngr-ready` first-boot completion marker path is now the single `host_setup.MNGR_READY_MARKER_PATH` constant, shared by both bootstrap renderers and the poller.

## create_host: pre-create validation runs before any provider write

- `VpsDockerProvider.create_host` now calls the `_validate_provider_args_for_create` hook before the first provider API write (the SSH key upload), instead of partway through `_provision_vps`. This means a provider-specific pre-create precondition that fails -- e.g. GCP's missing-firewall check -- aborts cleanly with no instance created, no SSH key uploaded, and no `Host creation failed, attempting cleanup...` path. The hook's docstring now reflects this contract (cheap, local or single read-only check, before any write). Behaviorally a no-op for providers whose hook is the default no-op or a cheap local guard (AWS's pytest auto-shutdown check).

## 2026-06-14

Agent discovery on VPS Docker providers (AWS, OVH, Vultr) now reads agents **live** from each host's container instead of from the persisted `agents/*.json` outer store. The outer store is only written by the host-side mngr at agent-create time, so agents created *inside* a container (for example by an in-container `mngr create`) were never recorded there and were invisible to `mngr message`, `mngr connect`, and any other command that resolves agents through discovery -- even though `mngr list` showed them (it already read live). This caused, among other things, onboarding messages to an in-container chat agent to never be delivered. Discovery now uses the same live read that imbue_cloud already used, and derives each host's running state from that same read (removing a separate per-host inspect round-trip). If only the live read fails (for example a `docker exec` racing a container restart) after a host's record has already been read, that host still appears in the listing as offline rather than disappearing.

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
