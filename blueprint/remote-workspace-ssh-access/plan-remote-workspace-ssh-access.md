# Remote workspace SSH access from any installation

## Overview

- After unlocking with the master password, an installation should be able to fully use the account's cloud workspaces — today the synced records carry the SSH private key and known_hosts, but no code ever materializes them, so a fresh install can see a leased host (the connector lists it) yet never authenticate to it.
- Scope is imbue_cloud only. AWS/Vultr records carry no SSH material (their keys live in a provider-wide `keys/` layout the collector never harvests) and their discovery needs cloud API credentials that do not travel in workspace records; Modal sandboxes are ephemeral (2-minute timeout). These stay explicitly out of scope.
- The core is a new secrets consumer, symmetric to the existing backup-env materializer: decrypt each cloud row's synced secrets and write the per-host `ssh_key` (mode 0600) into the imbue_cloud provider's expected per-host directory, plus merge known_hosts entries. Discovery then picks the key up lazily — the workspace flips from an inert "remote" tile to a fully usable one.
- Freshness is solved on both ends: consumers re-materialize on every reconcile pass (decrypt + compare + write-if-different, self-healing), and producers re-push secrets whenever the underlying plaintext actually changes (tracked by a local content hash) instead of today's push-once-when-absent.
- All new tile states (connecting / unreachable / sync error) are derived locally at render time — no wire-format changes, no server migration, no new persisted flags.

## Expected behavior

- Unlocking an account on a fresh install synchronously materializes that account's cloud-row SSH material, kicks discovery (only when something was actually written), and the post-unlock page immediately shows the affected tiles as "connecting…".
- Within one discovery cycle, each leased host the key grants access to appears in local discovery, and its tile swaps live (the landing page's existing drift-detection reload) from a remote tile to a normal, fully functional workspace tile.
- Once accessible, the workspace has full lifecycle parity from any unlocked install: connect, stop, start, and destroy all work; destroy from a non-leasing install releases the account-scoped lease with no extra confirmation step.
- A cloud tile whose host stops appearing in a healthy discovery snapshot shows an "unreachable" chip (it is not tombstoned); it keeps today's remote-tile actions (backup badge/download, "Remove from this list"). "Connecting…" resolves at the next imbue_cloud discovery snapshot: host listed → accessible; healthy snapshot without it → unreachable.
- If decrypting or writing a record's material fails, the tile shows a "sync error" chip with the failure detail in a hover tooltip; the state clears on the next successful pass. Errors are logged as warnings only (no Sentry event — the chip and log suffice).
- Accounts whose provider block is disabled still get files materialized, but their tiles show no connecting/unreachable chips — they stay plain remote tiles until the provider is re-enabled.
- Changes made on the hosting/leasing install (rotated restic env, new key) now re-sync: other unlocked installs converge on the next reconcile pass without any user action.
- Destroying a workspace (from anywhere) or removing its record deletes the materialized per-host key directory here; backups stay accessible via the separately materialized restic env. Signout leaves materialized material in place (profile owns its state; re-signin regains access without re-unlock).
- Locked accounts behave exactly as today (unlock banner, metadata-only tiles); metadata-only accounts (no master password anywhere) sync no secrets and are unaffected.

## Changes

- Consumer: a "materialize SSH material from records" step in the workspace record store, run for every unlocked account on unlock (synchronously for the unlocking account) and on every reconcile pass. Targets `providers/imbue_cloud/<instance>/hosts/<host_id>/` where the instance name is derived locally from the record's account email (`imbue_cloud_provider_name_for_account`) — never trusted from the wire.
- Overwrite policy in that step: the synced key wins unless this install leased the host itself (`lease.json` present in the host dir); the placeholder keypairs the provider generates for key-less discovered leases are replaced. known_hosts is merged add-if-absent per entry (same semantics as the provider's `_ensure_host_key_pinned`), never replacing connector-fed pins.
- The same pass eagerly materializes the backup env (`backup_envs/<agent_id>.env`), replacing today's lazy-only materialization, so both secrets consumers stay consistent and self-heal.
- Cleanup: tombstoned/removed records delete the whole materialized `hosts/<host_id>/` dir (only when it has no `lease.json`); each reconcile pass also sweeps orphaned per-host dirs (no `lease.json`, no active record) so records deleted while the app was closed do not leak key material.
- Producer freshness: the hosting/leasing install keeps a local plaintext content-hash per record and re-pushes secrets when the hash changes (today `_refresh_local_metadata` only re-adds secrets when the record has none).
- Discovery kick: after a materialization that wrote at least one new/changed key, bounce the observe child (the existing SIGHUP mechanism) so the flip does not wait for the next poll.
- Tile states: extend the remote-tile assembly and the landing page's existing polling/drift-reload path with three derived, local-only states — "connecting…" (key present, no resolving snapshot yet), "unreachable" (healthy snapshot lacks the host), and "sync error" (chip + tooltip from the in-memory last-error per record, sent to the frontend only). Chips are suppressed while the account's provider block is disabled.
- Tests: unit/integration only, in the existing two-device fake style (`test_workspace_sync.py`): materialization happy path, overwrite/ownership guard, known_hosts merge, tombstone + orphan-sweep cleanup, hash-triggered re-push, disabled-provider suppression, and derived tile-state computation.
- Docs: new spec at `specs/workspace-sync/remote-access.md`; `specs/workspace-sync/spec.md`'s consumer section links to it. One changelog entry; ships as a single branch/PR.
