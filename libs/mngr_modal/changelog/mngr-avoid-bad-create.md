## Modal provider no longer auto-creates an environment from non-create commands

`mngr list`, `mngr gc`, and other read flows no longer silently bootstrap a
Modal environment (the `Created Modal environment: ...` log line) just because
the modal provider is enabled. The Modal provider now disables itself (raises
`ProviderUnavailableError`, which higher-level loaders skip) when its per-user
Modal environment doesn't exist yet. Only `mngr create` is allowed to bootstrap
the environment on first use.
