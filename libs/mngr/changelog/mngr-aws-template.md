Fixed environment-variable forwarding for remote streaming SSH commands
(`OuterHost.execute_streaming_command` with `env=...`). The streaming path
prepended env vars as a bare `KEY=VAL command` prefix, which in the shell only
applies to the single simple command it precedes -- so for a compound command
like `install && tool ...` the var was gone by the time the second command ran.
It now uses `export KEY=VAL && command` (mirroring the non-streaming pyinfra
path), so the var is set in the shell environment for the whole command. This is
what made remote `depot build` fail with "missing API token" even though
`DEPOT_TOKEN` was supplied via `env`. Extracted the prefixing into a pure
`_prepend_env_exports` helper with unit tests.

Also fixed provider-config parsing to coerce field types. `_parse_providers` used
`model_construct`, which stored raw TOML scalars without coercion -- so an enum
field like `builder = "DEPOT"` stayed the string `"DEPOT"` and a `tuple` field
stayed a list. That tripped pydantic serializer warnings on `model_dump` and, for
a provider block defined in a single config layer (no merge to re-coerce it),
broke identity checks like `builder is DockerBuilder.DEPOT` (silently falling
back to the non-depot path). It now uses `model_validate`, which coerces while
still recording only the provided keys in `model_fields_set` so per-field
config-layer merging is unaffected. This also coerces nested-model provider
fields (e.g. SSH static `hosts` tables to `SSHHostConfig`), subsuming the
dedicated post-`model_construct` coercion helper that previously handled only
that case.
