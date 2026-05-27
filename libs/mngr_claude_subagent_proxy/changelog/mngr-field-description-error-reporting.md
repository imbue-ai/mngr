# Release test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False` and the
`MNGR_ALLOW_PYTEST` escape hatch was removed. The release-only
`test_real_claude_subagent` helper hand-rolls its own mngr profile, so it now
writes `is_allowed_in_pytest = true` into that profile's settings.toml to opt
into the pytest run. Test-only change; no user-facing behavior change.
