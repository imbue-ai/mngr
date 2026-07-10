minds.log is no longer flooded with repeated "Discovery error from ..." warnings for unauthorized providers: the forward stream consumer logs each provider-level discovery error once per process (with a note that repeats are suppressed) and an info-level recovery line when the provider's discovery next succeeds. Host- and agent-attributed discovery errors keep logging on every occurrence.

The bootstrap now always writes all four `[providers.aws-<region>]` blocks into the mngr profile settings, regardless of whether AWS credentials are present -- a credential-less region shows a discovery error in the providers panel (like vultr/ovh/gcp) instead of being silently absent. The rewrite preserves a panel-toggled `is_enabled` on these blocks.

The `[providers.modal]` block gets the same treatment: a panel-toggled `is_enabled = false` no longer triggers a rewrite on every startup (which previously reset it back to enabled) and is carried over when a rewrite does happen.

`_ensure_mngr_settings` now reports whether it modified the settings file, and the signin path bounces `mngr observe` when the provider set changed for any reason (previously only a per-account block change triggered the bounce).
