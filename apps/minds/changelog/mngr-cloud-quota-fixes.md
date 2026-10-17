Over-quota backup storage becomes self-service, and quota errors stop wasting retries.

- New "Free up backup space" action on the Accounts page (shown when the account is over its backup-storage quota): under a connector cleanup grant it removes the oldest half of each locally-reachable workspace's backups (never the latest), prunes, re-measures, and repeats until the account is back under quota. Runs immediately on click with live progress on the page and an OS notification when done; backups that only exist on another machine are reported by name.

- Backup provisioning treats the connector's structured quota refusal as terminal instead of retrying it for the whole retry budget, so the "backup setup failed" notification (now carrying the quota message) appears right away.

- Restoring a backup with a storage-quota-downgraded (read-only) key now works: restic restore retries once with `--no-lock` when the failure is specifically the repository lock write.

- The deploy.toml plans guard test now discovers tiers from disk (any new `envs/*/deploy.toml` is automatically covered) instead of a hardcoded list.

- New deployment test exercising the full storage-enforcement cycle (downgrade, cleanup grant, settlement) against a live ci env.
