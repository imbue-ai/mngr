# Handoff: host-backup "second snapshot failed" investigation

## Goal

On the live staging bare-metal slice, the **host-backup** service's *second* hourly snapshot
reported a failure. Figure out whether the host backup is actually broken (no snapshot produced)
or whether the snapshot succeeded and the error is spurious, then fix the root cause.

Ignore `runtime-backup` entirely (it's legacy/pointless here). This is only about `host-backup`
and its outer-helper **snapshot** path. Note also that host-backup's *restic* path is a separate
mechanism that is being skipped on this host (`missing RESTIC_REPOSITORY/RESTIC_PASSWORD`) -- that
is NOT the focus; the focus is the outer-helper snapshot, which DID run.

## The evidence (observed 2026-06-15, ~16:00Z)

`host-backup` runs in tmux `minds-staging-system-services:11` (`svc-host-backup`) inside the slice
container. It sends an outer-helper snapshot request roughly hourly. Two requests were sent:

- `14:50:21Z` -- op=snapshot (first run; its `result.json` was overwritten, outcome not captured)
- `15:50:36Z` -- op=snapshot (the "second run")

The container's `/mngr-snapshot/` had:
- `request.json` (15:50): `{"request_id":"2026-06-15T15:50:36.832057Z","operation":"snapshot",...}`
- `result.json` (written 15:52): 
  ```json
  { "request_id": "2026-06-15T15:50:36.832057Z", "operation": "snapshot",
    "exit_code": 1, "stdout": "",
    "stderr": "snapshot path already exists: 2026-06-15T15:50:36.832057Z",
    "snapshot_path": "" }
  ```

So the result reported `exit_code 1`, empty `snapshot_path`, stderr "snapshot path already exists".

## Leading hypothesis (start here)

The outer snapshot helper **intentionally fails when the target snapshot path already exists** --
it never reuses/overwrites. See `libs/mngr_vps_docker/imbue/mngr_vps_docker/_snapshot_helper_test.py:127`
("A snapshot request whose target path already exists fails, never reusing it"). The snapshot path
is derived from the `request_id` (the timestamp).

For the 15:50 path to "already exist", that exact request_id must have been snapshotted before -- i.e.
the **same request was processed twice**. Most likely the first processing of the 15:50 request
**succeeded and created the snapshot**, and a second processing (re-poll of an un-consumed
`request.json`, a helper retry, or two helper instances) then hit the existing path and wrote the
failing `result.json` we see -- masking the real success.

=> First determine whether a snapshot for `2026-06-15T15:50:...` (and for 14:50) actually exists. If
it does, the data is fine and the bug is "the helper re-processes a request and clobbers result.json
with a false failure." If it does NOT, it's a genuine collision/failure to dig into.

## Access (how to get onto the box / VM / container)

- **Vault** (read pool key + creds): `export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200 VAULT_NAMESPACE=admin` (operator must be `vault login`'d). Pool SSH key: `vault kv get -format=json -mount=secrets minds/staging/pool-ssh | jq -r .data.data.POOL_SSH_PRIVATE_KEY` -> write to a 0600 file.
- **Box (OVH dedicated):** `15.204.52.75` (serviceName `ns1018782.ip-15-204-52.us`), staging OVH account, datacenter `hil`. bare_metal_servers row id `21ae4720-7000-4a63-a81d-5a2fb335548e`.
- **Slice container sshd:** box-forwarded port **22001**, user `root`. The host-backup service + `/mngr-snapshot/` live here.
  `ssh -i <poolkey> -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 22001 root@15.204.52.75`
- **Slice lima VM root sshd (the "outer host" where the snapshot helper runs for a slice):** box-forwarded port **22000**, user `root`. The outer helper, `/var/lib/mngr-snapshot/`, and the btrfs snapshot store live here.
  `ssh -i <poolkey> ... -p 22000 root@15.204.52.75`
- The lima VM instance is `mngr-slice-5ef6e8e29a46435b83780f56c1f9b96f` (service user `limahost` on the box; `limactl list` as `limahost@15.204.52.75:22` shows it). The leased workspace is `minds-staging-fast-1`; the services run in the `minds-staging-system-services` agent.

## Mechanism + code pointers

The container/outer split lives in `libs/mngr_vps_docker/imbue/mngr_vps_docker/`:
- `container_setup.py` -- the mount-path constants:
  - `SNAPSHOT_TRIGGER_MOUNT_PATH = /mngr-snapshot` (container side; where host-backup drops `request.json` and reads `result.json`)
  - `OUTER_SNAPSHOT_TRIGGER_DIR = /var/lib/mngr-snapshot` (outer/VM side of that same dir)
  - `SNAPSHOT_READ_MOUNT_PATH = /mngr-snapshots` (container can READ completed snapshots here)
  - `OUTER_HELPER_ENV_PATH = /etc/mngr-snapshot-helper.env`
- `_snapshot_helper*.py` (+ `_snapshot_helper_test.py`) -- the OUTER helper that consumes a request and runs `btrfs subvolume snapshot`; contains the "already exists -> fail, never reuse" rule.
- `instance.py:~1039` -- `btrfs subvolume snapshot` against the per-host subvolume; comments on the `/mngr-snapshot/` bind.
- `_outer_helpers*.py` -- outer-side command execution.

host-backup itself (the requester + result poller) is in the FCT, not the monorepo:
`forever-claude-template/libs/host_backup/` (`runner.py`, `snapshot.py`). `snapshot.py:_do_request`
writes `request.json` and waits for `result.json`. Worktree: `.external_worktrees/forever-claude-template/`
(branch `mngr/bare-metal-staging`). Slice-side wiring of the helper: `mngr_imbue_cloud/providers/slice_provider.py`
and `slices/lima_slice_client.py` (how the helper is installed/launched on the lima VM vs the OVH VPS path).

## Concrete next steps

1. **Did snapshots actually get created?** In the container: `ls -la /mngr-snapshots/`. On the VM (port 22000): find the btrfs snapshot store (likely under the btrfs mount, e.g. `/mngr-btrfs/snapshots/`) and `btrfs subvolume list` / `ls` it. Look for entries named with the 14:50 and 15:50 request_ids. If 15:50 is present -> the snapshot succeeded; the failure is a re-process artifact.
2. **Why "already exists"?** Read `_snapshot_helper.py` to see (a) exactly how the snapshot path/name is derived from the request, and (b) the path-exists guard. Then read how the helper is *driven*: does it consume/clear `request.json` after processing? Is it a poll loop that could re-read the same request? Could two helper instances run (one per agent restart on lease)? Check the VM for the running helper process(es) and any helper log.
3. **Inspect the outer trigger dir on the VM:** `/var/lib/mngr-snapshot/` -- is there a stale/un-consumed `request.json`, a lock, or leftover state that would cause re-processing?
4. **Confirm the first (14:50) outcome** if any archived result/log exists (helper log on the VM), to see whether run 1 succeeded and run 2 is the only failure.
5. Once root cause is known, fix in the right layer: if it's re-processing/clobbering, fix the helper's request consumption + don't overwrite a success `result.json` with a re-process failure; if it's a genuine naming collision, fix the path derivation. The slice path and OVH-VPS path share `_snapshot_helper`, so verify the fix holds for both.

## Open questions

- Is the "already exists" purely a double-process artifact (snapshot is actually fine), or a real failure (no snapshot)? Step 1 answers this and determines severity.
- For slices specifically: is the outer helper correctly installed/single-instance on the lima VM? (The OVH-VPS path is the original; the slice path reuses `vps_docker` -- confirm the helper wiring carried over.)
- Snapshot retention/rotation: see `blueprint/host-backup-snapshot-rotation/` -- a rotation/prune step could also interact with "already exists".

## State / caveats

- The box is real and billing (~$83/mo, staging OVH). The slice is leased by the `minds-staging-fast-1` workspace.
- Do NOT rely on the operator's shared `~/.ssh/known_hosts` (use `StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null`); container/VM host keys are ephemeral and box ports get reused.
- Vault token: the operator's `vault login` session must be valid; if vault reads start failing, that's why.
