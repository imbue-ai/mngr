# Adopt-session test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False`, so a
config loaded during a pytest run must opt in. The `mngr_claude`
adopt-session tests hand-roll a trusted-subprocess profile and load it, so the
`trusted_subprocess_env` fixture now writes `is_allowed_in_pytest = true` into
that profile's settings.local.toml. Test-only change; no user-facing behavior
change.
