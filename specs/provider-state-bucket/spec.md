# Provider state buckets (S3 / Azure Blob) for offline-readable mngr state

Status: **Proposed.** Branch: `mngr/volumes`. Captures the design for giving the AWS and Azure
providers a cloud object-storage bucket that holds mngr's control-plane state, so a stopped/offline
instance's host record, agent metadata, and `host_dir` can all be read without SSH and without
hitting the EC2/VM tag character limit. This is the deferred "S3-backed offline metadata" future
work named in `specs/aws-ec2-stop-start-lifecycle/spec.md` (Phase 5 / "Out of scope / future"),
generalized to Azure and extended to cover `host_dir` (the Lima-style offline volume read).

## Goal

Three concrete asks (user-stated):

0. **`mngr aws prepare` / `mngr azure prepare` create a bucket** (S3 bucket for AWS; a Blob
   container in a storage account for Azure) to hold mngr state. GCP is **not** changed — its
   per-instance metadata allowance is generous enough that it needs no external store.
1. **(a)** Move the per-agent tag mirror ("sketchy tag info") off instance tags and into the bucket,
   so the 256-char EC2/VM tag-value limit stops silently dropping data.
2. **(b)** Make the full host record readable while the instance is offline (today only a lossy
   tag subset survives a stop). Investigation result is in "Background" below: today host records
   are **not** durably readable offline on AWS/Azure/GCP beyond the thin tag mirror.
3. **(c)** Add a Lima-style option to make `host_dir` readable while the instance is offline,
   **on by default** (matching Lima's `is_host_data_volume_exposed=True`).

## Background: current state and the gap

The AWS/Azure/GCP providers are built on the shared `mngr_vps_docker` base (`VpsDockerProvider`).

- **The full host record** (`VpsDockerHostRecord`: `certified_host_data`, `vps_ip`, SSH host keys,
  container config + id) lives in `host_state.json` on the in-instance btrfs subvolume
  (`libs/mngr_vps_docker/imbue/mngr_vps_docker/host_store.py`). It is read **over SSH while the
  instance runs**. When the instance is stopped, none of it is reachable.
- **The offline tag mirror.** To keep `mngr list` working while stopped, AWS and Azure mirror a
  subset into instance tags via the `persist_agent_data` / `list_persisted_agent_data_for_host`
  hooks: `mngr-host-id`, `mngr-provider`, `Name=mngr-<name>`, `mngr-created-at`, and per-agent
  `mngr-agent-<id>-{name,type,labels}`. EC2/Azure tag **values cap at 256 chars**; oversized values
  (notably the `labels` JSON) are **dropped with a warning** — the "sketchy tag info." There is no
  chunking. (`libs/mngr_aws/imbue/mngr_aws/backend.py`, `libs/mngr_azure/imbue/mngr_azure/backend.py`.)
- **GCP** uses instance **metadata** instead of labels (no practical size cap), so it is unaffected
  and is intentionally out of scope.
- **`host_dir` offline read.** Lima exposes `host_dir` to the host filesystem (default
  `is_host_data_volume_exposed=True`) so `mngr event` / `mngr transcript` work while the VM is
  stopped, via `get_volume_for_host()` -> `OfflineHostWithVolume`
  (`libs/mngr/imbue/mngr/hosts/offline_host.py`). Modal achieves the same with a persistent Modal
  Volume read over the Modal API. **AWS/Azure have no `get_volume_for_host()` today** — a stopped
  instance's `host_dir` is unreadable.

## The reference designs (Lima + Modal)

- **Lima:** `host_dir` is bind-mounted to the host; offline reads come straight from the host
  filesystem. On by default.
- **Modal:** two stores, both decoupled from compute and therefore readable offline "for free":
  - a **per-host volume** holding `host_dir`, mounted into the sandbox and written **live by the
    instance** (implicit creds via the mount) + a 60 s `sync` daemon + a graceful-shutdown commit;
  - a **shared per-app state volume** holding host records + agent data, written **by the mngr host
    machine** via the Modal API with the operator's token.
  - Offline reads use `get_volume_for_host()` -> `OfflineHostWithVolume` (host_dir) and a volume
    `read_file` of `/hosts/<id>.json` (record).

S3/Blob are the AWS/Azure analog of "compute-decoupled storage readable via API." The one place we
diverge from Modal: Modal's container gets volume access implicitly from the mount; S3/Blob do not
auto-mount, so the **instance-push of `host_dir` requires a provisioned cloud identity** (IAM
instance profile / Azure managed identity). The host-record/agent-metadata writes do **not** —
those come from the mngr host machine using the operator's existing credentials, exactly like
Modal's state volume.

## Decisions

### Locked (confirmed with the user)

1. **The bucket is the single, complete source of truth for state content.** Write the complete
   `VpsDockerHostRecord` (which already contains host-id, provider, host name, `vps_instance_id`, and
   all per-agent records) to the bucket, keyed by host id. **No information lives only in tags.** The
   instance retains only a tiny, immutable **index** derived from the record — `mngr-host-id`,
   `mngr-provider`, `Name` (console readability), and `mngr-created-at` — because instance
   enumeration + power-state still comes from `DescribeInstances` / VM list (which returns tags, not
   bucket contents) and must map instance <-> bucket object. These index tags are a strict subset
   that never overflows; they are a foreign key, not a competing store. (A pure-bucket variant that
   drops even `mngr-host-id` and joins the cloud instance list against bucket records on
   `vps_instance_id` is possible; we keep the host-id index tag for robustness — an instance that
   exists before its record lands is still identifiable.)
1a. **When the bucket is present, drop the per-agent `mngr-agent-<id>-*` tags entirely.** The state
   bucket is written by the mngr *host machine* with operator credentials, so it is available
   whenever `prepare` created it — **independent of the (c) `host_dir`-sync toggle or any backup
   setting**. Agent name/type/labels therefore live *only* in the bucket; we stop writing per-agent
   tags. This removes both failure modes in today's code: the 256-char `labels` drop
   (`mngr_aws/backend.py` `_agent_field_tags`) and the `TagLimitExceeded` ->
   `NotImplementedError` at EC2's 50-tag ceiling (`persist_agent_data`). The legacy per-agent tag
   mirror is retained **only as a fallback when no bucket is configured** (operator never ran
   `prepare`, or an older `prepare`), so behavior degrades gracefully rather than regressing.
   Fixes (a) and (b) together. `[CLAUDE: superseded — the bucket is now required infrastructure with
   no tag-mirror fallback. The per-agent tag mirror was removed entirely; when the bucket has not been
   provisioned, the provider raises an actionable "run `mngr <provider> prepare`" error on the
   create/label write path as well as on offline reads, and transient bucket errors propagate.]`
2. **`host_dir` is synced to the bucket by the instance (instance-push)**, the Modal model: an
   on-box daemon syncs `host_dir` to the bucket periodically and on graceful stop; offline reads are
   served from the bucket via a new bucket-backed volume. **On by default** (matches Lima).
3. **`prepare` provisions the bucket-write identity, best-effort**: an AWS IAM role +
   instance profile, and an Azure user-assigned managed identity + a `Storage Blob Data Contributor`
   role assignment on the container (mirrors the existing `mngr-self-deallocate` custom-role pattern
   in `mngr_azure`). Identity provisioning is gated on the `is_offline_host_dir_enabled` provider
   config field (default `True`): when on, `prepare` **warns and continues** if it lacks the
   permissions rather than failing the whole command; set it to `False` to skip the identity step.
   `prepare` is idempotent: re-running after a bucket-only prepare adds just the
   identity (bucket creation no-ops). `[CLAUDE: superseded — the tri-state flag was replaced by the
   is_offline_host_dir_enabled config field (default True). The bucket is now required, so prepare no
   longer "warns and continues": a bucket-create failure fails the command, and when
   is_offline_host_dir_enabled is on an identity-provisioning failure fails it too. Re-running prepare
   after flipping the field false->true adds just the identity.]`

### Proposed (please confirm — these shape most of the implementation)

4. **Native object storage, not restic, not the imbue_cloud R2 abstraction.** Use `boto3` (already a
   dependency) for S3 and `azure-storage-blob` (new dependency) for Blob. Rationale: the state must
   be **plainly mngr-readable** offline via `get_object` / blob download. The existing restic
   `host-backup` service (`specs/host-backup`) is an **orthogonal, encrypted DR backup** of `/mngr`
   to R2, orchestrated by minds and gated behind a per-workspace password held minds-side; reusing
   it would force `mngr` core to shell out to `restic restore` with a password it does not own — the
   wrong layer. The `mngr_imbue_cloud` R2 bucket abstraction is Cloudflare-connector-specific (API
   tokens, not native AWS/Azure creds) and does not apply.
5. **`host_dir` sync = periodic `aws s3 sync` / `az storage blob sync` + final sync on stop**, not a
   FUSE mount (mountpoint-s3 / blobfuse). Periodic incremental sync mirrors Modal's 60 s `sync`
   daemon, is robust to crashes (offline data is "as of last sync"), and avoids a FUSE dependency in
   the image.
   - **Btrfs-snapshot-before-sync was descoped (as-built).** Earlier drafts of this decision had the
     sync read a consistent btrfs snapshot (via the existing host-backup helper) first. The
     implementation syncs the **live** `host_dir` tree directly on both providers, and does not touch
     the `mngr_vps_docker` snapshot helper. Rationale: the stop-time sync runs *after* the container
     is stopped (`super().stop_host` first), so `host_dir` is already quiesced and the final copy is
     consistent without a snapshot; the periodic sync accepts the same "as-of-last-sync" freshness a
     snapshot would not improve on. Taking a snapshot would mean wiring into the helper's
     event/docker-volume-triggered protocol for no correctness gain. If a future need for a strictly
     point-in-time periodic copy arises, reuse that helper.
6. **`prepare` reintroduces IAM/identity provisioning** that `aws-ec2-stop-start-lifecycle`
   deliberately removed (it made `prepare` security-group-only). The bucket-write role is the reason.
   To keep `prepare` usable for operators without IAM permissions, identity provisioning is
   **best-effort and gated on the `is_offline_host_dir_enabled` provider config field** (default
   `True`):
   - When `is_offline_host_dir_enabled` is on (default): attempt to provision the identity; on a
     missing-permission / API failure, **log a warning and continue** — the bucket (a/b) is still set
     up, only offline `host_dir` (c) is unavailable until `prepare` is re-run with sufficient
     permissions.
   - When `is_offline_host_dir_enabled` is off: do not attempt identity provisioning at all
     (bucket-only prepare).
   The bucket-only steps are unconditional and idempotent, so re-running `prepare` (once sufficient
   permissions are granted) adds just the missing identity. `[CLAUDE: superseded — the tri-state flag
   `--use-offline-host-dir {yes,auto,no}` was replaced by the is_offline_host_dir_enabled config
   field (default True). The bucket is now required infrastructure, so prepare is no longer
   best-effort: a missing bucket permission fails the command, and when is_offline_host_dir_enabled is
   on a missing identity permission fails it too (rather than warning and continuing). "Skip the
   identity" is expressed by setting the field to False.]`
7. **Offline `host_dir` detects a missing identity and tells the user to re-run `prepare`.** When
   `get_volume_for_host` is used against a host whose instance was never granted the bucket-write
   identity, we detect it directly from cloud state — AWS: `DescribeInstances`'
   `IamInstanceProfile` association is absent (and/or the role/instance-profile does not exist in
   IAM); Azure: the VM has no assigned managed identity / role assignment — and raise a clear error:
   re-run `mngr <provider> prepare` with sufficient IAM/permissions (and recreate/restart the host so
   it picks up the identity). This avoids silently returning an empty/stale volume. `[CLAUDE:
   superseded — the original text referenced `mngr <provider> prepare --use-offline-host-dir yes`; the
   tri-state flag was replaced by the is_offline_host_dir_enabled config field (default True), so the
   re-run is just `mngr <provider> prepare` with sufficient permissions]`

### Defaults (will implement unless you object)

8. **Bucket identity & naming.** One **shared** bucket per `prepare` scope (per region for AWS; per
   resource-group+region for Azure), objects keyed by host id under `hosts/<host_id_hex>/`.
   - AWS S3 bucket names are global + DNS-form (3–63, lowercase): `mngr-state-<account_id>-<region>`.
   - Azure storage-account names are global (3–24, lowercase alphanumeric): `mngrst<short-hash>`
     derived from subscription+resource-group; Blob container `mngr-state`.
   - Bucket creation is idempotent (reuse if present, tagged `managed-by=mngr`).
9. **`cleanup`** deletes the bucket + identity, but **refuses if any managed host still has state**
   in it (mirrors the existing cleanup safety that refuses to delete the SG while instances exist).
10. **Encryption on** (S3 SSE-S3 / Blob service-side encryption), bucket/container **private**,
   public access blocked. Per-host object prefixes; the instance role is scoped to the bucket (a
   tighter per-host prefix scope via session policy is a possible hardening follow-up).

## Architecture

Object layout in the bucket, per host:

```
hosts/<host_id_hex>/host_state.json        # full VpsDockerHostRecord (written by host machine)
hosts/<host_id_hex>/agents/<agent_id>.json # per-agent records (written by host machine)
hosts/<host_id_hex>/host_dir/...           # host_dir mirror (pushed by the instance)
```

### Component 1 — bucket client abstraction (shared)

A small `VolumeInterface`-compatible object-store wrapper so the offline-read path can reuse the
existing `OfflineHostWithVolume` machinery (the same interface `ModalVolume` implements):

- `libs/mngr_aws/.../state_bucket.py`: `S3StateBucket` wrapping `boto3` (`put_object`, `get_object`,
  `list_objects_v2`, `delete_object`) and an `S3Volume(VolumeInterface)` for `host_dir` reads
  (`read_file`, `listdir`, `write_files`).
- `libs/mngr_azure/.../state_bucket.py`: `BlobStateBucket` + `BlobVolume` over `azure-storage-blob`.
- Both expose: `write_host_record`, `read_host_record`, `write_agent_record`,
  `list_agent_records`, `delete_host_state`, and `volume_for_host() -> VolumeInterface`.

(If a genuinely provider-agnostic surface emerges, factor a tiny `StateBucketInterface` into
`mngr_vps_docker`. Start provider-local to avoid premature abstraction.)

### Component 2 — host record + agent metadata in the bucket (fixes a, b)

- When a state bucket is configured, rewrite `persist_agent_data` / `remove_persisted_agent_data` /
  `list_persisted_agent_data_for_host` (and the offline host-record reconstruction) in the
  `mngr_aws` / `mngr_azure` backends to read / write the bucket via Component 1, written by the
  **mngr host machine** (operator creds) on create and on every host-record update (at minimum on
  stop), exactly when tags are written today. In this mode the per-agent `mngr-agent-<id>-*` tags
  are **not written at all** (Decision 1a) — removing the 256-char drop and the 50-tag ceiling.
- **Graceful fallback:** when no bucket is configured, keep today's per-agent tag mirror unchanged
  (including its documented limits), so existing deployments that never ran the new `prepare` keep
  working. `[CLAUDE: superseded — the bucket is now required and the tag-mirror fallback was removed;
  with no bucket the provider raises a "run `mngr <provider> prepare`" error on reads and writes alike.]`
- Keep writing the **index tags** (`mngr-host-id`, `mngr-provider`, `Name`, `mngr-created-at`) in
  both modes so instance enumeration + power-state detection are unchanged.
- Offline discovery reconstructs the full `OfflineHost` from `host_state.json` in the bucket (read
  by host id resolved from the index tags), instead of the lossy tag subset. No 256-char limit.

### Component 3 — `host_dir` offline volume (fixes c)

- **On-box sync daemon** installed over SSH from the post-create finalize hook (the same pattern as
  the existing AWS idle-watcher systemd install — *not* cloud-init), as a systemd `.timer` + oneshot
  `.service` in the provider override. Each tick runs `aws s3 sync` / `azcopy sync` of the live
  `host_dir` tree to `hosts/<id>/host_dir/` (see Decision 5 — no btrfs snapshot; the tree is synced
  directly). Interval configurable (default 60 s). Also triggered synchronously on graceful stop
  (after the container is stopped, so the final copy is consistent) before the instance powers off.
- **Credentials:** the instance profile / managed identity from Decision 3 grants the daemon
  bucket-write. No long-lived keys on the box.
- **Offline read:** `AwsProvider.get_volume_for_host` / `AzureProvider.get_volume_for_host` return
  the `S3Volume` / `BlobVolume` for the host when the instance is not online; core's
  `make_readable_offline_host()` already upgrades the offline host to `OfflineHostWithVolume` when a
  volume is available (`libs/mngr/imbue/mngr/hosts/offline_host.py`). `mngr event` / `mngr transcript`
  then work against a stopped instance.
- **Default on**; a provider config flag (`is_offline_host_dir_enabled`, default `True`)
  disables it, matching Lima's `is_host_data_volume_exposed` knob.

### Component 4 — `prepare` / `cleanup`

- `prepare`: idempotently create the bucket/container (Decision 8), then provision the bucket-write
  identity when `is_offline_host_dir_enabled` is on (Decisions 3 & 6).
  Read-only-first where possible (head/list before create), matching the existing SG-prepare style.
  When the feature is on, an identity-provisioning permission / API error fails the command; when
  off, the identity step is bypassed entirely. The bucket steps always run, are idempotent, and a
  bucket-create failure fails the command. `[CLAUDE: superseded the earlier "error is swallowed into
  a warning" wording — the bucket is required, so prepare now fails on a bucket or (feature-on)
  identity provisioning error.]`
- `cleanup`: delete identity + bucket, refusing while managed-host state remains (Decision 9).
- Offline `host_dir` read (Decision 7) first checks that the host's instance actually has the
  identity attached (AWS `DescribeInstances.IamInstanceProfile`; Azure VM identity) and raises a
  clear "re-run `mngr <provider> prepare` (with sufficient IAM/permissions)" error if not, instead of
  returning an empty volume.
- New IAM perms for `prepare`: S3 `CreateBucket`/`PutBucketPolicy`/`PutEncryptionConfiguration` +
  IAM `CreateRole`/`PutRolePolicy`/`CreateInstanceProfile`/`PassRole`; Azure storage-account
  create + role-assignment write + managed-identity create. When `is_offline_host_dir_enabled` is on,
  missing IAM perms fail the command (the bucket is required, so prepare does not fall back to a
  bucket-only run). Per-host create path additionally needs `iam:PassRole` for the instance profile
  (AWS) / identity assignment (Azure).

## Data model notes

- No change to `VpsDockerHostRecord` shape; it is serialized as-is into `host_state.json` in the
  bucket (same JSON already written on-volume).
- `VpsHostConfig` already carries `vps_instance_id` and (AWS) the optional `iam_instance_profile`;
  the provisioned profile/identity name is recorded so create can attach it and cleanup can remove
  it.
- New provider-config fields: `state_bucket_name` (override; default derived), and
  `is_offline_host_dir_enabled` (default `True`).

## Testing

- Unit: bucket-name derivation; the `S3StateBucket`/`BlobStateBucket` record round-trips (against a
  mocked/`moto` S3 and the Azure SDK's in-memory/stubbed client); discovery reconstruction from a
  bucket record; pure generators for the sync daemon's unit/script bodies.
- Integration/release (double-gated like the existing AWS/Azure release tests): full
  create -> stop -> read-offline cycle asserting the host record **and** `host_dir` are readable with
  the instance stopped, and that an agent `labels` blob larger than 256 chars survives a stop
  (the regression the tag limit causes today). `prepare` -> `cleanup` bucket+identity lifecycle.
- Ratchets / `test_no_type_errors` as usual; `azure-storage-blob` added to `mngr_azure` deps.

## Out of scope / future

- **GCP** (metadata allowance is sufficient).
- **Per-host-prefix-scoped instance credentials** (session-policy hardening) — start bucket-scoped.
- **FUSE mount** of the bucket as `host_dir` (mountpoint-s3 / blobfuse) instead of periodic sync.
- **Convergence with the restic `host-backup` service** — kept orthogonal; this is control-plane
  state, that is encrypted user-data DR.
- **Migrating the existing on-volume `host_state.json`** away (it stays as the authoritative
  online copy; the bucket is the offline mirror + the agent-metadata store).

## Risks / open questions

- **`host_dir` size / sync cost.** Periodic full-tree `s3 sync` of a large `host_dir` could be slow
  or costly; mitigated by incremental sync + excludes. Confirm acceptable default interval (60 s)
  and whether large transient files (e.g. build caches) should be excluded by default.
- **Crash freshness.** Instance-push means an ungraceful crash leaves `host_dir` "as of last sync"
  (same tradeoff Modal accepts). Acceptable per Decision 2.
- **Reintroducing IAM into `prepare`** (Decisions 6 & 7) — resolved: identity provisioning is gated
  on the `is_offline_host_dir_enabled` config field (default `True`); setting the field to `False`
  opts out. `[CLAUDE: superseded the earlier "best-effort, degrading to a warning" resolution — the
  bucket is now required, so with the feature on a missing-IAM-permission failure fails `prepare`. The
  offline `host_dir` *read* still degrades gracefully: a genuinely empty host_dir prefix returns no
  volume with a non-fatal "re-run prepare" diagnostic; only a bucket probe error propagates.]`
- **Azure storage-account global-name collisions** — derive deterministically from
  subscription+RG+region and surface a clear error / override on collision.
