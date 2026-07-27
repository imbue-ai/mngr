# Backup retention for destroyed workspaces

When a workspace is destroyed, its backups are not deleted immediately: they are kept for **30 days** and then reaped automatically. Within that window you can download them or delete them early; after it, they are gone for good. There is no opt-out -- downloading within the window is the escape hatch.

The retention window is a fixed constant served by the connector (`GET /policies/destroyed-workspace-backups`); the desktop client falls back to 30 days when the endpoint is unreachable.

## What is kept, and what reaping deletes

A destroyed workspace's backup consists of three things:

- its R2 **bucket** (imbue_cloud backups only; named `<account-prefix>--<host-id>`),

- its synced **workspace record** (the tombstone, `state = destroyed`, stamped with `destroyed_at` by the connector),

- the local canonical **`restic.env`** under the minds data dir.

Reaping deletes them in strict order -- bucket first (emptied client-side, then destroyed), then the record, then the env -- so a failed bucket delete always leaves the row for a retry, and nothing is ever half-deleted from the user's point of view.

Bring-your-own backends (your own S3 bucket via an API key) are never reaped at the repository level: only the record and the local env are cleaned up; your data stays until you remove it.

## The two reapers

- **Client-side** (all workspace types: docker, lima, imbue_cloud, BYO-cloud): a reap pass runs every 30 minutes inside the desktop client's sync loop (plus once shortly after startup), capped at a handful of reaps per pass. It skips any backup with an in-flight download. Reap and eviction events are recorded under `events/backup_reaper/events.jsonl` in the minds data dir.

- **Server-side backstop** (imbue_cloud only): the connector runs an hourly reap cron so leftovers are reclaimed even when no client is running. It also ages *orphan* buckets (workspace-backup buckets no record references) from a first-seen stamp -- which doubles as a rollout grace period for pre-existing leftovers.

Both reapers are idempotent, so they never conflict; whoever runs first wins.

Any record tombstoned for longer than the window is reapable regardless of how it was tombstoned (explicit destroy or absence detection) -- the window itself is the safety margin for local workspaces, and a workspace that reappears in discovery is resurrected (clearing the clock) before it can ever be reaped.

## Quota-pressure eviction

Backup provisioning for a new workspace can hit the account's bucket-count or storage quota. Instead of failing, the client **evicts**: it force-destroys the oldest destroyed workspace's backup (something the reapers would delete anyway -- past-window first, then within-window early if needed) and retries, looping until provisioning succeeds or nothing is left to evict. Eviction is silent (the Recently destroyed page reflects it); live workspaces' data is never touched automatically. A destroy failure aborts the attempt with the usual "Backup setup failed" notification.

The same eviction applies when enabling backups later from workspace settings, so a quota-stuck workspace self-heals through the existing "enable backups" action.

## The "Recently destroyed workspaces" page

Linked from the bottom-left of the workspace list (`/workspaces/destroyed`):

- every tombstoned workspace still inside the window, newest first, with a days-until-deletion countdown;

- **Download** (the latest snapshot as a zip) -- works from any signed-in device that can decrypt the record's synced secrets; rows needing the sync master password show an unlock hint;

- **Delete backup now** (with an inline confirmation) -- frees quota before the window expires;

- orphan backups this device holds with no record ("this device" rows) with the same affordances (delete requires the owning account to be signed in).

Restoring is not offered -- these workspaces no longer exist; the download is the escape hatch.

## Related knobs

- minds-managed mngr profiles set `default_destroyed_host_persisted_seconds = 30 days` (mngr's own default is 7), so destroyed mngr host records age out together with the backups.

- Ops: `POST /admin/sweep/backup-retention` on the connector runs a reap pass on demand (`?dry_run=1` lists candidates; `?window_seconds=0` reaps fresh tombstones -- used by the deployment test). `mngr imbue_cloud bucket destroy <name> --force` is the manual per-bucket lever.
