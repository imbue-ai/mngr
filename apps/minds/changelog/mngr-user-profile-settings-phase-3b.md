User-profile-state phase 3B, the workspace boundary (spec sections 7.4, 7.5, and 8):

- The per-workspace update facts move into each workspace: the skipped update target and the update-row dismissals now live beside `data/.state/update-apply/schedule.json` (written through the update program's `read`, which doubles as the write-through for queued changes), and the backup-verification flag lives as `verification_disabled` in the workspace's `data/system/backup.toml` (written through the new `backup_verification_flag` program). Every computer managing a workspace now agrees on them.

- Each install keeps a per-workspace facts cache that answers while a workspace is stopped and queues changes until a write-through lands; a one-shot CLEANUP-marked migration seeds it from the old per-install stores (`[updates.skipped_targets]` in config.toml, `update_dismissals/`, `backup_verification_disabled/`) and removes them.

- Backup checks now read the verification flag from the workspace itself: the check program answers the flag first and skips the expensive work when verification is off, so a flag flipped on another computer is honored on the next check.

- The desktop materializes the workspace profile snapshot (`data/.state/profile/account.json`: the workspace's account and owner; no setting keys yet) on create right after the welcome chat is seeded, on sync passes whose resolved content moved, and on association change. The creating client also writes `data/.state/user_timezone` once, when absent; the `/api/v1/timezone` route stays, CLEANUP-marked, for older templates.

- The snapshot's format and keys are published as `workspace_profile_contract.json`, which the template vendors and pins its reader against.
