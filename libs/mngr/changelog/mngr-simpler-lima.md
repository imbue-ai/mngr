# Docstring: update stale provider-field example

`ProviderInstanceConfig.merge_with` used `is_host_in_docker` as an illustrative
provider field in its docstring. That field was removed from the Lima provider
(which no longer runs agents in a nested Docker container); the example now
references `is_run_as_root`. No behavior change.
