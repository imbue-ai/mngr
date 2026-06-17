# AWS EC2 stop/start lifecycle (idle-pause + resume)

Status: **Phases 1, 2, and 4 implemented; Phases 3, 5 pending.** Captures the design agreed while
planning how to give AWS agents a Modal-like "idle-paused but resumable" lifecycle. Branch:
`mngr/aws-stop`. Landed: native EC2 stop/start (`AwsVpsClient.stop_instance`/`start_instance` + the
`AwsProvider.stop_host`/`start_host` overrides, `mngr stop --stop-host`); EC2-tag offline
discovery so a stopped host still lists its agents and resolves by name (Phase 4 — required for
Phase 1 to be usable); and the self-stopping idle watcher (Phase 2 — **no-IAM** design: sentinel +
host-side systemd path unit that powers the host off, with EC2's `InstanceInitiatedShutdownBehavior`
deciding stop-vs-terminate). Covered by unit tests plus a `mngr stop --stop-host` -> `start` release
test and an idle-watcher auto-stop/resume release test. Pending: the GC stop-instead-of-destroy +
age-gated terminate (Phase 3), and an S3-backed agent store for many-agent hosts (Phase 5 / Phase-4
follow-up).

## Goal

When an AWS agent goes idle, the EC2 instance should **stop** (compute billing ends, the EBS
root volume and all state persist), and `mngr start` should **resume** it. This mirrors the
Modal lifecycle in spirit: idle → cheap, resumable. Unlike Modal, AWS has a native stop/start
primitive that preserves the volume, so we use that directly instead of Modal's
snapshot-and-recreate dance.

## Background: current state and the gap

The AWS provider (`libs/mngr_aws`) is built on the shared `mngr_vps_docker` base
(`VpsDockerProvider`), alongside Vultr, OVH, and imbue_cloud.

- `mngr stop` / `mngr start` and the on-host idle watcher act **only on the Docker container**
  inside the EC2 box. The on-host `shutdown.sh` is literally `kill -TERM 1`
  (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py:1178`), which stops the container
  from the inside. **The EC2 instance and EBS volume keep running and billing the whole time**,
  so idle-pause saves almost nothing today.
- The "destructive backstop" is the control-plane GC (`libs/mngr/imbue/mngr/api/gc.py`):
  after a host sits agent-less past a grace period, `destroy_host` →
  `terminate_instances` → the EBS volume is deleted (`DeleteOnTermination=True`). The agent is
  gone with no way back. GC is **invocation-driven** (`mngr gc`, and post-`destroy`/`cleanup`),
  not a daemon.
- Host/agent records live on the EBS volume and are read by **SSHing into the VPS**
  (`_read_records_from_vps`). This works today only because the instance always stays running.

## The Modal model (for reference)

Modal has no stop/resume primitive, so `stop_host` = `snapshot_filesystem()` → terminate; resume
= build a new sandbox from the snapshot. Idle is detected by an **in-sandbox** `activity_watcher.sh`
that POSTs to a **deployed Modal web endpoint** (`snapshot_and_shutdown`) which holds Modal API
credentials and performs the privileged snapshot+terminate on the sandbox's behalf. Host/agent
records live on a **separate, always-on persistent Modal Volume**, which is why a paused Modal
host can still list its agents while offline.

The pattern to copy: **the box triggers; a credentialed identity outside the box executes the
privileged action.** On AWS, the "credentialed identity" is an **IAM instance role** delivered via
IMDS — no separate endpoint needed.

## Decisions (locked)

1. **Native EC2 stop/start** for idle-pause and `mngr stop`. Not Modal-style snapshot+recreate.
   The EBS volume is preserved across the stop; resume is `StartInstances`.
2. **Drop EBS snapshots entirely** for now. They are not load-bearing once nothing auto-terminates
   (the volume always survives). May be added later as a separate backup/clone feature.
3. **Idle-stop trigger = self-stopping instance** (the faithful Modal analog in spirit). An
   outer-host watcher reacts to an in-container idle signal and stops the instance.
   Not control-plane-only (which would require a cron running `mngr gc` and has no Modal analog).
   **[Revised in implementation — see Phase 2.]** The originally-locked mechanism was "watcher calls
   `ec2 stop-instances` on itself via an IMDS-provided IAM role". The shipped design drops IAM
   entirely: the watcher powers the host off (`shutdown -P now`) and EC2's
   `InstanceInitiatedShutdownBehavior` (config `terminate_on_shutdown`, default `stop`) decides
   stop-vs-terminate. This removes the `mngr-aws` IAM role, the `iam:PassRole` requirement, and the
   awscli install, at the cost of the single-flag tradeoff documented in Phase 2 (an instance is
   either resumable-on-idle OR instance-autonomously self-terminating, not both; we chose resumable
   + control-plane/GC reaping).
4. **Backstop = stop on idle, auto-terminate only after a long retention (matching Modal).** Idle
   → stop (volume preserved, resumable). GC acts **non-destructively**: it *stops* a reachable
   idle/agent-less AWS host rather than terminating it (GC option (ii) below). A host that then
   stays stopped/idle beyond a retention window (default ~7 days, mirroring Modal's
   `destroyed_host_persisted_seconds`) is **auto-terminated** to reclaim the volume — the analog of
   Modal's age-gated `gc_snapshots` cleanup. Manual `mngr destroy` terminates immediately. The
   `auto_shutdown_seconds` pytest leak-safety net stays (release tests only).
5. **Offline metadata = EC2 tags only** (host-level) for now. Paused hosts list with correct
   name/state/idle info; their agents reappear only after resume. S3-backed full parity (agents
   listable while paused, like Modal) is noted as a possible follow-up, not built now.
6. **Changes contained in `mngr_aws`, with one additive default-false base seam.** `AwsProvider`
   already holds its own concretely-typed `aws_client: AwsVpsClient` (separate from the base's
   `vps_client: VpsClientInterface`), so `stop_instance`/`start_instance` live entirely on
   `AwsVpsClient` and are called from `AwsProvider` — `VpsClientInterface` and the
   `VpsDockerProvider` lifecycle bodies are untouched. `AwsProvider` **overrides**
   `stop_host`/`start_host` and delegates the container work to `super()`, so the base bodies are
   unchanged and Vultr/OVH/imbue_cloud behavior is byte-for-byte identical. The **one** anticipated
   base touchpoint is in core `mngr` (not `VpsDockerProvider`): Phase 3 adds a default-false
   `should_gc_stop_instead_of_destroy` capability hook consulted at the single `destroy_host` call
   site in `gc.py`. It is additive and matches the existing `supports_*` property pattern; every
   provider that doesn't opt in keeps today's behavior.
7. **IP handling = accept-and-update.** A stopped instance loses its public IP; on resume we read
   the new IP and update the host record + known_hosts. Elastic IP allocation is a fallback only
   if accept-and-update proves messy in practice.

## Architecture

### Phase 1 — Instance stop/start (contained in `mngr_aws`)

- `AwsVpsClient` (`libs/mngr_aws/imbue/mngr_aws/client.py`): add `stop_instance(instance_id)`
  (`ec2 stop-instances`, wait for `stopped`) and `start_instance(instance_id) -> new_ip`
  (`ec2 start-instances`, wait for `running`, return the new public IP). These are AWS-only
  methods; `VpsClientInterface` is **not** modified (AwsProvider calls `self.aws_client.…`).
- `AwsProvider.stop_host` (override): call `super().stop_host(host, create_snapshot=False,
  stop_reason=STOPPED)` to stop the container and update the record in a single write (no
  docker-commit — the filesystem persists on the stopped volume; the `stop_reason` rides along in
  that write), then `self.aws_client.stop_instance(...)`.
- `AwsProvider.start_host` (override): `self.aws_client.start_instance(...)` first (instance must
  be running before we can SSH), persist the new `vps_ip` into the host record + refresh
  known_hosts, then call `super().start_host(...)` to start the container against the refreshed IP.
- The base `VpsDockerProvider.stop_host`/`start_host` bodies are unchanged; other providers are
  unaffected.
- New IAM permissions for the per-host path: `ec2:StopInstances`, `ec2:StartInstances`.

### Phase 2 — Self-stopping idle watcher (no-IAM poweroff) — IMPLEMENTED

Implemented as a **sentinel + host-side systemd path unit that powers the host off**, rather than
the originally-sketched self-stop-via-IAM-role approach. A container cannot power off its host, so
the in-container watcher only *signals* idle and the outer host does the poweroff. **No IAM role,
instance profile, `iam:PassRole`, or awscli is involved** — the watcher never calls the EC2 API:

- `AwsProvider._create_shutdown_script` overrides the base (which runs `kill -TERM 1`) to write an
  in-container `shutdown.sh` that **touches a sentinel file** (`stop-instance-requested`) under
  `host_dir/commands/` on the shared volume instead of stopping the container. The existing
  in-container `activity_watcher.sh` invokes this `shutdown.sh` on idle unchanged.
- `AwsProvider._on_host_finalized` installs the host-side watcher (after the host record is durably
  written): it writes a systemd `mngr-aws-idle-watcher.path` unit whose `PathExists=` points at the
  **outer-filesystem** location of that sentinel (`<btrfs_mount_path>/<host_id_hex>/host_dir/
  commands/stop-instance-requested`). When the sentinel appears, the paired oneshot
  `mngr-aws-idle-watcher.service` runs `/bin/sh -c 'rm -f <sentinel> && shutdown -P now'` — it
  removes the sentinel (so a later resume isn't immediately re-stopped) and powers the host off.
  Generators for the three unit/script bodies are pure module functions
  (`_build_sentinel_shutdown_script`, `_build_idle_watcher_path_unit`,
  `_build_idle_watcher_service_unit`) covered by unit tests. The whole install is best-effort and
  **must not raise** (per the base hook contract): any failure logs a warning and the host comes up
  with no auto-stop (manual `mngr stop --stop-host` still works).
- **Stop-vs-terminate is decided by `InstanceInitiatedShutdownBehavior`,** set from the new
  `terminate_on_shutdown` config field (default `false` → `stop`, i.e. the resumable idle-pause;
  `true` → `terminate`, ephemeral / self-cleaning). The watcher's `shutdown -P now` powers the OS
  off; EC2 then applies this launch-time flag. `mngr aws prepare`/`cleanup` are now
  **security-group-only** (no IAM provisioning); `create_instance` attaches no default profile, only
  the optional operator-supplied `iam_instance_profile` when set.
- **Tradeoff (single launch-time flag).** `InstanceInitiatedShutdownBehavior` is one value chosen at
  launch and governs *all* OS-initiated shutdowns on the instance — both the idle watcher's poweroff
  and the `auto_shutdown_seconds` time-cap poweroff. Without an IAM role, the watcher cannot
  selectively call `stop-instances` vs `terminate-instances`; it can only power off and let the flag
  decide. So an instance is either **instance-autonomously self-terminating** (`terminate`) OR
  **resumable on idle** (`stop`), not both. We chose `stop` (resumable idle-pause, the Modal analog)
  as the default and lean on the control-plane / GC reaping (Phase 3) and manual `mngr destroy` for
  eventual cleanup of abandoned stopped instances. Release tests opt into `terminate` so a leaked
  test instance auto-destroys at the time cap (except the resumable-idle-stop test, which uses
  `stop` and relies on the session-end leak scanner).
- **Security:** the instance metadata hop limit is still set to 1
  (`MetadataOptions.HttpPutResponseHopLimit = 1`) as a hardening default so a hostile container
  cannot reach IMDS. With the no-IAM design there is no role credential to protect, but the hop
  limit stays as defense-in-depth.

### Phase 3 — Backstop = stop, never auto-terminate

Context: the **GC-driven destroy** (`libs/mngr/imbue/mngr/api/gc.py`, `_gc_single_host`) **is** the
"destructive backstop" the user flagged. Today, when an AWS agent exits, the container stops but
the EC2 instance stays online with no agents; after `get_min_online_host_age_seconds` of quiet, GC
calls `destroy_host` → `terminate_instances` → the volume is deleted with no snapshot. Decision #4
requires this to stop happening for AWS. Note GC is invocation-driven (`mngr gc`, post-destroy/
cleanup) and skips any host it cannot reach (a stopped instance), so the only window where GC could
terminate is between an agent exiting and the idle watcher stopping the instance.

**Chosen: (ii) GC stops instead of destroys, plus an age-gated auto-terminate.** This mirrors
Modal most closely (its idle teardown is non-destructive; a separate age-gated pass does eventual
cleanup).

- **GC stop (non-destructive backstop).** At the single `provider.destroy_host(host)` call site in
  `_gc_single_host`, when the provider opts in via a new default-False hook (e.g.
  `should_gc_stop_instead_of_destroy`), call `stop_host` instead of `destroy_host`. AWS opts in;
  every other provider keeps destroying (unchanged). This makes GC stop a reachable idle/agent-less
  AWS host (cost-safe, volume kept, resumable) instead of terminating it.
- **Age-gated auto-terminate (~7 days).** A GC pass terminates a host that has stayed stopped/idle
  beyond the retention window (default mirrors `destroyed_host_persisted_seconds` ≈ 7 days),
  reclaiming the volume. Because a stopped EC2 instance is unreachable, this decision must run off
  metadata that survives the stop (EC2 tags / `DescribeInstances` launch+stop timestamps), not an
  SSH read. Exact mechanism is Phase-3 design work; it is the analog of Modal's `gc_snapshots`.

Manual `mngr destroy` terminates immediately. Keep the `auto_shutdown_seconds` time-cap mechanism
as the release-test leak backstop; whether the cap stops or terminates the instance now follows the
`terminate_on_shutdown` flag (release tests set it `true` so the cap self-terminates). It is
independent of the API-driven stop.

### Phase 4 — Offline metadata via EC2 tags (REQUIRED for resume-by-name, not optional)

**Finding (Phase 1 follow-up):** a fully-stopped EC2 instance has no public IP, so it drops out of
`AwsProvider._list_provider_vps_hostnames` and therefore out of discovery entirely. That breaks
`mngr start <name>`: name resolution goes through discovery (`find_all_agents` →
`discover_hosts_and_agents`), so a stopped host can't be resolved by name and can't be started. So
Phase 4 is a hard prerequisite for Phase 1 to be user-usable, not a nice-to-have.

Two sub-parts, with an open decision on the second:

1. **Host-level discovery from tags.** Surface stopped instances in discovery by reconstructing a
   `DiscoveredHost` (host_id from the `mngr-host-id` tag, host_name from the `Name=mngr-<name>` tag)
   without SSH. `start_host` already resolves the instance by tag, so once the host is discoverable
   the resume path works.
2. **Agent-level resolution while stopped (the open fork).** `mngr start` is agent-addressed
   (`mngr start --host` is `NotImplementedError`), so resolving `mngr start <agent>` needs the
   stopped host's *agents* to be discoverable. But agent records live on the unreadable EBS volume.
   Options: (a) persist a minimal agent record (id/name/type/labels, as per-field
   `mngr-agent-<id>-<field>` tags) into EC2 tags and serve it via `list_persisted_agent_data_for_host`
   (no new infra; capped by EC2's 50-tag / 256-char limits, so fine for few-agent hosts -- a host with
   too many agents to mirror raises `NotImplementedError`, prompting an issue); (b) persist agent
   records to S3 (full parity, any count; the
   previously-deferred piece); or (c) implement `mngr start --host <name>` so resume targets the
   host and skips agent resolution. Modal's analog is (a/b): it serves stopped-host agents via
   `list_persisted_agent_data_for_host` backed by its persistent volume.

- On create and on every host-record update (at least on stop), write the host-level record into
  EC2 tags: host name, `stop_reason`, `created_at`, and a compact idle config (timeout, sources).
  `mngr-host-id`, `mngr-provider`, `mngr-created-at` already exist.
- AWS discovery builds a `DiscoveredHost` + offline `HostDetails` from `DescribeInstances` tags
  when the instance is stopped (no SSH), so paused hosts stay visible in `mngr list` with the
  correct state. When the instance is running, keep the current SSH-based read (authoritative,
  includes agents). Agents are not surfaced while stopped (accepted limitation; see future work).

### Phase 5 — Tests, docs, changelog

- Unit tests for the new client methods and the capability gating; integration/release tests for
  a full stop → resume cycle (behind the existing `MNGR_AWS_RELEASE_TESTS` double-gate). Verify
  paused hosts list correctly from tags with the instance stopped.
- Update `libs/mngr_aws/README.md` (lifecycle, the SG-only `prepare`/`cleanup`, the no-IAM idle
  watcher and the `terminate_on_shutdown` flag, IMDS hop limit) and the relevant `libs/mngr/docs`
  lifecycle/idle pages.
- Changelog entries for every project touched: `mngr_aws`, `mngr_vps_docker`, and `mngr` if base
  GC/interface code changes; `dev` for this spec.

## Data model notes

- `VpsHostConfig` already persists `vps_instance_id`, which is all we need to call
  stop/start-instances. `vps_ip` in `VpsDockerHostRecord` becomes mutable across a stop/start and
  is refreshed on resume.
- No new snapshot records (EBS snapshots dropped). `stop_reason` already exists on
  `CertifiedHostData` and drives the offline `STOPPED` state derivation
  (`supports_shutdown_hosts=True` is already set for vps_docker).

## Out of scope / future

- **EBS snapshots** (manual `mngr snapshot` backed by real EBS snapshots; no `AwsVpsClient`
  snapshot surface exists today). Revisit if backups/clone-from-snapshot are wanted.
- **S3/SSM-backed offline metadata** so paused hosts list their agents like Modal, via the
  existing `persist_agent_data` / `list_persisted_agent_data_for_host` hooks. Would add an S3
  bucket (provisioned in `mngr aws prepare`) + `s3:*Object`/`ListBucket` IAM perms.
- **Elastic IP** for a stable address across stop/start (~$3.60/mo per idle EIP).

## Risks / open questions

- Resume latency: EC2 cold start is ~30–60s plus cloud-init re-run considerations — confirm the
  watcher and container come back cleanly after a real stop/start (not just reboot).
- Confirm `start-instances` reliably returns a usable public IP in the default-VPC + auto-assign
  configuration we provision; otherwise EIP becomes necessary sooner.
- ~~Exact IAM policy condition for self-scoped `StopInstances`~~ — moot: the no-IAM poweroff design
  dropped the self-stop IAM role entirely (see Phase 2). The remaining tradeoff is the single
  `InstanceInitiatedShutdownBehavior` flag (resumable-on-idle OR self-terminating, not both).
- GC opt-out mechanism: confirm the cleanest way to make AWS exempt from GC-driven destroy
  without weakening GC for other providers.
