# Workspace stop/start (imbue_cloud)

An imbue_cloud workspace can now be stopped without holding its bare-metal
slot: `mngr stop` halts the slice's lima VM, uploads its disks (encrypted)
to the tier's OVH Object Storage bucket, and frees the slot once the upload
verifies; `mngr start` restores it -- near-instantly on its origin box
within the local-retention window, or by downloading onto any same-region
box with a free slot after it. The minds desktop shows the Start/Stop
control for imbue_cloud workspaces through the same gate as local ones.

The pool_hosts row is the workspace's stable identity across the whole
lifecycle (`host_db_id` and `host_id` never change):

```
running (leased) -> stopping -> stopped -> starting -> running
```

- `stopping`: VM halted (the user already sees STOPPED), upload in flight,
  slot still held; a restart in this window cancels the stop in place.
- `stopped`: artifact verified in the bucket, VM deleted, slot freed.
- `starting`: a connector supervisor is restoring/booting it.
- `crashed`: operator-abandoned (`mngr imbue_cloud admin workspaces abandon`);
  the user recovers by restoring the workspace's backup.

## Moving parts

- **Connector** (`apps/remote_service_connector`): `GET /workspaces` (the
  full-lifecycle listing; `GET /hosts` stays leased-only and is deprecated),
  `POST /workspaces/{id}/stop|start` (async, 202 + poll), a per-transition
  Modal supervisor (0.25 CPU / 512MB) that drives the box-side scripts and
  finalizes DB state, and an hourly watchdog cron that re-spawns supervisors
  for rows whose heartbeat went stale. Retries continue indefinitely;
  abandoning a row is always a manual operator action.
- **Boxes**: prep installs pinned `age` + `s5cmd`; transfers run as detached
  scripts (`zstd | age | s5cmd`) reporting through a status file. The
  boot-time slice autostart skips VMs carrying the stop-requested marker so
  a box reboot never resurrects a half-uploaded VM.
- **Artifact**: the slice's self-contained qcow2 `disk` + `datadisk`, plus a
  small metadata tar (`lima.yaml` and sidecars), keyed under
  `[<env>/]<host-id>/gen-<n>/`. Each object is encrypted to a per-stop age
  identity; the identity is wrapped by the tier KEK and stored on the row
  (committed *before* any byte uploads). Ciphertext sha256s live in the DB
  manifest and are verified before boot. Re-stops keep only the newest
  generation; destroy deletes the workspace's objects immediately (the
  restic backup's 30-day retention remains the safety net).
- **Quotas**: `max_remote_workspaces` caps *running* workspaces
  (leased/stopping/starting); the larger `max_total_workspaces` (explorer
  10 / ally 50) caps running + stopped. Stopping is always allowed; create
  checks both caps; start re-checks the running cap.

## Provisioning a tier (operator, once)

One bucket + one S3 user per tier. Dev envs share the dev tier's bucket --
the deploy stamps each env's `WORKSPACE_STORAGE_KEY_PREFIX=<env>/` override
automatically, so per-env artifacts (and their cleanup) stay disjoint.

1. Create an S3 user + credentials in the tier's OVH cloud project
   (`role=objectstore_operator`; `POST /cloud/project/<id>/user` then
   `POST .../user/<uid>/s3Credentials` via the OVH API, creds from the
   tier's `ovh` Vault entry).
2. Create the bucket (e.g. `mngr-workspaces-<tier>`) against the tier
   region's endpoint, e.g. `https://s3.us-east-va.io.cloud.ovh.us`
   (standard/`io` class; measured download from boxes ~1 GB/s).
3. Generate the KEK: `openssl rand -base64 32`.
4. Populate Vault from the template and deploy:

```bash
cp .minds/template/storage.sh /tmp/storage-<tier>.sh
$EDITOR /tmp/storage-<tier>.sh
uv run scripts/push_vault_from_file.py <tier> storage /tmp/storage-<tier>.sh
shred -u /tmp/storage-<tier>.sh
eval "$(uv run minds env activate <tier>)"
uv run minds env deploy --yes-i-mean-<tier>
```

`storage` is in every tier's `deploy.toml` services list, so the deploy
pushes it as the `storage-<env>` Modal secret the connector reads. A dev
env without the Vault entry populated still deploys (the deploy logs an
error and pushes a placeholder secret); the deployed connector then
cleanly refuses stop/start with a 503 until the entry is populated and
the env redeployed. Staging / production deploys hard-fail when the
entry is missing or misses template-declared keys -- push the entry
first (empty values are allowed to deliberately leave the feature
disabled). The dev tier's entry is populated (bucket
`mngr-workspaces-dev`).

## Operations

- `mngr imbue_cloud admin workspaces abandon <host-db-id> --reason ...`
  marks a row on a permanently dead box `crashed` (retries stop; the user
  restores from backup; artifacts are reclaimed at release). Releasing a
  crashed row attempts the VM teardown best-effort: an unreachable box is
  logged and the release still completes, and if the box was actually alive
  the leftover VM surfaces in the box-reconcile sweep.
- A stuck transition is visible as a `stopping`/`starting` row with a stale
  `transition_heartbeat_at` plus connector logs; the watchdog re-drives it
  hourly forever.
- KEK rotation re-wraps the per-stop identities in the DB only -- objects
  are never re-encrypted.
- Known constraint: upload from boxes to OVH Object Storage is currently
  throttled server-side (~6-25 MB/s regardless of parallelism; download is
  ~1 GB/s), so slot reclaim after a stop takes tens of minutes. Tracked as
  a parallel ops investigation; content-addressed chunk dedupe (planned
  phase 2) cuts uploads to the workspace's unique bytes.

## End-to-end verification

`apps/minds/deployment_tests/test_workspace_stop_start.py` runs the full
lease -> stop -> (upload, slot freed) -> start -> SSH-verified restore ->
release cycle against a real env; it skips cleanly when the env has no
baked slice or no storage configured. Run it against a dev env with a
baked box:

```bash
just minds-test-services-against dev-<you> apps/minds/deployment_tests/test_workspace_stop_start.py
```

(The test is in the `minds_services` batch, so it needs the
services-against runner pointed at a deployed env;
`minds-test-deployment-only` runs only the `minds_deployment` batch and
would deselect it.)
