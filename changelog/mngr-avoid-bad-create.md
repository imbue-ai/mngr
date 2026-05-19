## Modal provider no longer auto-creates an environment from non-create commands

`mngr list`, `mngr gc`, and other read flows no longer silently bootstrap a
Modal environment (the `Created Modal environment: ...` log line) just because
the modal provider is enabled. The Modal provider now disables itself (raises
`ProviderUnavailableError`, which higher-level loaders skip) when its per-user
Modal environment doesn't exist yet. Only `mngr create` is allowed to bootstrap
the environment on first use.

This is plumbed through a new `is_for_host_creation: bool = False` parameter on
`ProviderBackendInterface.build_provider_instance` / `api.providers.get_provider_instance`,
which all other backends accept and ignore. `mngr create` passes `True`; every
other path leaves the default.
