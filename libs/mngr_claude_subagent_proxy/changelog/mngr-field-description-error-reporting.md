# Release test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False`, so a
config loaded during a pytest run must opt in. The release-only
`test_real_claude_subagent` helper hand-rolls its own mngr profile and loads it,
so it now writes `is_allowed_in_pytest = true` into that profile's settings.toml.
Test-only change; no user-facing behavior change.
