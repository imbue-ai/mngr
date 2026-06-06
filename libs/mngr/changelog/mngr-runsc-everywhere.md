Fixed `ProviderInstanceConfig.merge_with` so a higher-precedence config layer
only overrides the provider fields it actually set. It previously used an
"override wins unless its value is None" rule, which meant any field whose
default is a non-None value (a `bool` defaulting to `False`, an empty tuple,
etc.) was silently reset to that default whenever a higher layer touched the
provider block at all -- even via a single-key override like a create
template's `setting__extend = ["providers.<name>.is_enabled=true"]`.

Concretely, applying `providers.lima.is_enabled=true` (as the minds
forever-claude-template's lima create template does) reset `is_host_in_docker`,
`install_gvisor_runtime`, and `default_container_run_args` back to their
defaults, so the Lima provider silently ran in direct-in-VM mode instead of
docker-in-VM mode. The merge now uses `model_fields_set` (matching
`AgentTypeConfig` / `PluginConfig`), so untouched fields keep their base value.
