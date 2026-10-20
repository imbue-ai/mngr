Cut the minds-v0.4.4 release: bumped the app version to 0.4.4 and pinned `FALLBACK_BRANCH` to the `minds-v0.4.4` default-workspace-template tag, shipping everything landed on main since minds-v0.4.3.

The headline fix is the leased-here trust-material staleness fix: a device that leased a cloud workspace before another device adopted it (rotating the slice's sshd keys) no longer spins forever on "Loading workspace" -- the record-secrets materializer applies the synced adopted keys, and a bootstrap-drift escape hatch re-applies record known_hosts pins whose endpoints still hold only absent-or-bootstrap material.

Desktop-created shares now stamp the connector-reported chrome origin into the workspace's `share.env` (`SHARE_CHROME_ORIGIN`) instead of hardcoding the bare connector URL, so the hosted `/web` chrome can frame desktop-shared workspaces on tiers with a custom chrome domain (issue #746).
