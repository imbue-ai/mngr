`docs/deploy/reboot-resilience-rollout.md` is now marked complete (both sweeps ran fleet-wide at minds-v0.3.17): the removed `backfill-autostart` sweep references were rewritten as historical record, and the post-reboot recovery section now documents the manual per-VM re-apply of the template's installer block instead of the removed sweep tool.

Comment-only: the backend resolver's empty-`label` tolerance for legacy service rows (written before minds-v0.3.12 minted `<name>-<rand>` origin labels) is now marked with a `CLEANUP:` comment stating when it can be removed.
