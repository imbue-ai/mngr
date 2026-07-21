# Remote workspace SSH access from any installation

How an installation that never provisioned a cloud workspace becomes able to
fully use it (connect, stop, start, destroy) after unlocking the account with
the master password. Consumer counterpart to the secrets *assembly* described
in [spec.md](./spec.md).

## Scope

- imbue_cloud rows only. The lease is account-scoped and discovery works from
  any signed-in install (the provider queries the connector's `GET /hosts`);
  the one missing piece on a fresh install is the per-host SSH private key,
  which is generated client-side at lease time (only the public half reaches
  the connector) and travels in the record's encrypted secrets.
- aws / vultr / modal are explicitly out of scope: their records carry no SSH
  material (provider-wide `keys/` layouts the collector does not harvest),
  their discovery needs cloud API credentials that do not travel in workspace
  records, and modal sandboxes are ephemeral.

## Materialization (consumer)

`WorkspaceRecordStore.materialize_account_synced_secrets(user_id, email)`:

- Runs synchronously for the unlocking account in the unlock endpoint (so the
  post-unlock reload already renders "connecting"), and for every unlocked
  account on each sync-scheduler pass (after the reconcile).
- For each ACTIVE record: eagerly materializes the backup env (the previously
  lazy-only path), and for cloud rows decrypts the secrets and writes:
  - `providers/imbue_cloud/<instance>/hosts/<host_id>/ssh_key` (0600, atomic)
    plus the derived `ssh_key.pub` (mngr regenerates the pair when either half
    is missing, which would clobber the materialized key);
  - known_hosts entries merged add-if-absent per line -- the connector-fed
    pins that discovery records stay authoritative.
- The instance name is derived locally from the account email
  (`imbue_cloud_provider_name_for_account`), never trusted from the wire.
- Ownership guard: a host dir containing `lease.json` belongs to this install
  (it leased the host); the materializer never touches it. Placeholder
  keypairs (generated when discovery finds a lease without a local key) have
  no `lease.json` and are replaced.
- Compare-and-write: unchanged material touches nothing; deleted or corrupted
  files self-heal on the next pass. When anything was written, the caller
  bounces the detached `mngr observe` (SIGHUP) so the workspace becomes
  reachable now instead of on the next poll.
- Failures are recorded per workspace in memory (surfaced as a tile chip) and
  logged as warnings; the next pass retries.

## Cleanup

Each materialization pass sweeps per-host key dirs that have no `lease.json`
and no ACTIVE record -- covering workspaces destroyed elsewhere (tombstones),
records removed via the UI, and records deleted while the install was closed.
A recent-mtime grace (1h) protects an in-flight lease whose keypair exists
before its `lease.json` lands. Signout leaves materialized material in place
(the profile owns its state; re-signin regains access without re-unlock).

## Producer freshness

Records carry a local-only `secrets_content_hash` (digest of the plaintext
this device last contributed; never synced, preserved across pulls). The
reconcile's metadata refresh re-pushes secrets when the locally collectable
material's hash differs from the stored one. A device that never contributed
(hash unknown) never replaces another device's secrets with its own --
possibly partial -- view of the material.

## Derived tile states

Remote cloud tiles derive an access state at render time (no stored flags):

- `""` (plain): no materialized key yet (locked account / no synced key), or
  the account's provider block is disabled (chips suppressed).
- `error`: the last materialization attempt failed; detail in the tooltip;
  clears on the next successful pass.
- `connecting`: a key exists but no healthy provider snapshot has arrived
  since it appeared (also while the provider's last poll errored).
- `unreachable`: a healthy snapshot newer than the key lacks the host (lease
  expired/released, or the key does not grant access). Never tombstoned; the
  tile keeps the remote-tile actions (backup badge/download, remove).

The SSE `workspaces` event carries an `agent_id -> state` map for remote
tiles; the landing page reloads on drift (a state change, a tile flipping
into local discovery, or a new remote record), so the remote-to-accessible
flip happens live.

## Lifecycle parity

Once materialized, the install has full parity: connect, stop, start, and
destroy all work over SSH; destroy from a non-leasing install releases the
account-scoped lease (no extra confirmation).
