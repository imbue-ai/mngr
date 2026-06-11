# AWS EC2 stop/start lifecycle (idle-pause + resume)

Status: **spec only — not yet implemented.** Captures the design agreed while planning how to
give AWS agents a Modal-like "idle-paused but resumable" lifecycle. Branch: `mngr/ebs-snapshot`
(the name predates the decision below to drop EBS snapshots; the actual feature is native EC2
stop/start).

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
3. **Idle-stop trigger = self-stopping instance + IAM role** (the faithful Modal analog). An
   outer-host watcher calls `ec2 stop-instances` on itself via IMDS-provided role credentials.
   Not control-plane-only (which would require a cron running `mngr gc` and has no Modal analog).
4. **Backstop = stop, never auto-terminate.** Nothing tears the instance down automatically;
   stopped EBS volumes persist (and accrue storage cost) until a human runs `mngr destroy`. The
   existing `auto_shutdown_minutes` pytest leak-safety net stays (release tests only).
5. **Offline metadata = EC2 tags only** (host-level) for now. Paused hosts list with correct
   name/state/idle info; their agents reappear only after resume. S3-backed full parity (agents
   listable while paused, like Modal) is noted as a possible follow-up, not built now.
6. **All changes contained in `mngr_aws`; no base behavior change.** `AwsProvider` already holds
   its own concretely-typed `aws_client: AwsVpsClient` (separate from the base's
   `vps_client: VpsClientInterface`), so `stop_instance`/`start_instance` live entirely on
   `AwsVpsClient` and are called from `AwsProvider` — `VpsClientInterface` and the base are
   untouched. `AwsProvider` **overrides** `stop_host`/`start_host` and delegates the container
   work to `super()`, so the base bodies are unchanged and Vultr/OVH/imbue_cloud behavior is
   byte-for-byte identical. Only additive, no-op-by-default seams are added to the base if strictly
   required (currently none anticipated).
7. **IP handling = accept-and-update.** A stopped instance loses its public IP; on resume we read
   the new IP and update the host record + known_hosts. Elastic IP allocation is a fallback only
   if accept-and-update proves messy in practice.

## Architecture

### Phase 1 — Instance stop/start (contained in `mngr_aws`)

- `AwsVpsClient` (`libs/mngr_aws/imbue/mngr_aws/client.py`): add `stop_instance(instance_id)`
  (`ec2 stop-instances`, wait for `stopped`) and `start_instance(instance_id) -> new_ip`
  (`ec2 start-instances`, wait for `running`, return the new public IP). These are AWS-only
  methods; `VpsClientInterface` is **not** modified (AwsProvider calls `self.aws_client.…`).
- `AwsProvider.stop_host` (override): call `super().stop_host(host, create_snapshot=False)` to
  stop the container and update the record (no docker-commit — the filesystem persists on the
  stopped volume), set `stop_reason` (`PAUSED` vs `STOPPED`), then `self.aws_client.stop_instance(...)`.
- `AwsProvider.start_host` (override): `self.aws_client.start_instance(...)` first (instance must
  be running before we can SSH), persist the new `vps_ip` into the host record + refresh
  known_hosts, then call `super().start_host(...)` to start the container against the refreshed IP.
- The base `VpsDockerProvider.stop_host`/`start_host` bodies are unchanged; other providers are
  unaffected.
- New IAM permissions for the per-host path: `ec2:StopInstances`, `ec2:StartInstances`.

### Phase 2 — Self-stopping idle watcher + IAM (Modal analog)

- Install an **outer-host** idle watcher via cloud-init (a systemd unit/timer), reusing
  `activity_watcher.sh` logic, pointed at a `shutdown.sh` that runs
  `aws ec2 stop-instances --instance-ids <self-id-from-IMDS>` using the instance role. It reads
  the same activity files from the btrfs subvolume path on the host (`/mngr-btrfs/<host>/...`),
  not from inside the container.
- `mngr aws prepare` provisions a `mngr-aws` IAM role + instance profile with a self-scoped
  `ec2:StopInstances` policy (condition on `ec2:ResourceTag/mngr-host-id` matching, so a box can
  only stop mngr-managed instances). `create_host` attaches it by default (config still allows an
  override via `iam_instance_profile`). New `prepare`/admin IAM perms: `iam:CreateRole`,
  `iam:PutRolePolicy`, `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`,
  `iam:PassRole` (and the delete equivalents for `mngr aws cleanup`).
- **Security:** set the instance metadata hop limit to 1 (`MetadataOptions.HttpPutResponseHopLimit
  = 1`) so the container cannot reach IMDS and grab the role credentials. Only the trusted outer
  host (and thus the watcher) can.

### Phase 3 — Backstop = stop, never auto-terminate

Context: the **GC-driven destroy** (`libs/mngr/imbue/mngr/api/gc.py`, `_gc_single_host`) **is** the
"destructive backstop" the user flagged. Today, when an AWS agent exits, the container stops but
the EC2 instance stays online with no agents; after `get_min_online_host_age_seconds` of quiet, GC
calls `destroy_host` → `terminate_instances` → the volume is deleted with no snapshot. Decision #4
requires this to stop happening for AWS. Note GC is invocation-driven (`mngr gc`, post-destroy/
cleanup) and skips any host it cannot reach (a stopped instance), so the only window where GC could
terminate is between an agent exiting and the idle watcher stopping the instance.

Two candidate mechanisms (decision pending):

- **(i) AWS-local opt-out (zero core change).** `AwsProvider` overrides
  `get_min_online_host_age_seconds` to effectively never let GC destroy a reachable AWS host.
  Simplest and keeps all changes in `mngr_aws`. Tradeoff: GC also stops reaping genuinely-dead
  AWS hosts (FAILED/CRASHED), which would then require manual `mngr destroy`.
- **(ii) GC stops instead of destroys for stoppable providers (small gated core change).** At the
  one `provider.destroy_host(host)` call site in `_gc_single_host`, if the provider opts in (a new
  default-False `should_gc_stop_instead_of_destroy`), call `stop_host` instead. More robust (idle/
  orphaned hosts get stopped — cost-safe and resumable — rather than terminated) but touches core
  `mngr`. Other providers keep destroying.

Manual `mngr destroy` remains the only path that terminates the instance and deletes the volume in
either case. Keep the `auto_shutdown_minutes` + `InstanceInitiatedShutdownBehavior=terminate`
mechanism exactly as-is — it is independent of the API-driven stop and is the release-test leak
backstop.

### Phase 4 — Offline metadata via EC2 tags

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
- Update `libs/mngr_aws/README.md` (lifecycle, the new IAM role in `prepare`, the IAM perm list,
  IMDS hop limit) and the relevant `libs/mngr/docs` lifecycle/idle pages.
- Changelog entries for every project touched: `mngr_aws`, `mngr_vps_docker`, and `mngr` if base
  GC/interface code changes; `dev` for this spec.

## Data model notes

- `VpsHostConfig` already persists `vps_instance_id`, which is all we need to call
  stop/start-instances. `vps_ip` in `VpsDockerHostRecord` becomes mutable across a stop/start and
  is refreshed on resume.
- No new snapshot records (EBS snapshots dropped). `stop_reason` already exists on
  `CertifiedHostData` and drives the offline `PAUSED`/`STOPPED` state derivation
  (`supports_shutdown_hosts=True` is already set for vps_docker).

## Out of scope / future

- **EBS snapshots** (manual `mngr snapshot` backed by real EBS snapshots; the `AwsVpsClient`
  methods already exist but are unwired). Revisit if backups/clone-from-snapshot are wanted.
- **S3/SSM-backed offline metadata** so paused hosts list their agents like Modal, via the
  existing `persist_agent_data` / `list_persisted_agent_data_for_host` hooks. Would add an S3
  bucket (provisioned in `mngr aws prepare`) + `s3:*Object`/`ListBucket` IAM perms.
- **Elastic IP** for a stable address across stop/start (~$3.60/mo per idle EIP).

## Risks / open questions

- Resume latency: EC2 cold start is ~30–60s plus cloud-init re-run considerations — confirm the
  watcher and container come back cleanly after a real stop/start (not just reboot).
- Confirm `start-instances` reliably returns a usable public IP in the default-VPC + auto-assign
  configuration we provision; otherwise EIP becomes necessary sooner.
- Exact IAM policy condition for self-scoped `StopInstances` (resource-tag condition) needs
  validation against how RunInstances tags are applied at launch.
- GC opt-out mechanism: confirm the cleanest way to make AWS exempt from GC-driven destroy
  without weakening GC for other providers.
