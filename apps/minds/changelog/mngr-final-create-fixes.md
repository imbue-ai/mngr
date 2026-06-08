Final fixes to the standardized workspace-create flow:

- Region selection is now explicit. The create form always shows a "Region"
  control under advanced settings for providers that place a host in a region
  (Imbue Cloud and Vultr). It defaults to that provider's last-used region (saved
  per provider in `~/.minds/config.toml`), then a region guessed from your IP
  geolocation, then a hardcoded default (US-EAST-VA for Imbue Cloud, `ewr` for
  Vultr). The chosen region is remembered for next time on a successful create.
  The old, implicit "preferred region" behavior has been removed; geolocation is
  now fetched once at startup in the background instead of hourly.
- Backups no longer block workspace creation or get lost on slow hosts. Restic
  backup setup runs after the workspace is ready, retries for up to ~5 minutes if
  the host isn't reachable yet, and only notifies you if it ultimately fails.
- Destroyed workspaces now disappear from the workspace list, and destroying a
  workspace no longer reports a spurious "failed" once the host is actually gone.
