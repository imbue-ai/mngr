- Promoted minds 0.7.3 to the stable desktop channel (build `260925cpjm6e86d`) at 100% rollout, and to the stable web channel (`minds-v0.7.3`), with Josh's sign-off.

- Stable now publishes a Linux manifest: its `platforms` list gained `linux`, so `stable-linux.yml` exists and Linux desktops update from the feed rather than from ToDesktop's own.

- `release-channels.toml`'s promotion instructions name every `_RELEASE_CHANNEL_PLATFORMS` fallback the connector pins, not just the mac one, and its `platforms` note no longer describes stable as mac-only.
