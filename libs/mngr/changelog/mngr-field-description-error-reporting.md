# `is_allowed_in_pytest` now defaults to False, and the `MNGR_ALLOW_PYTEST` escape hatch is gone

The `is_allowed_in_pytest` config field now defaults to `False` (previously
`True`). During a pytest run, `load_config` refuses to run when a config file is
loaded that does not set `is_allowed_in_pytest = true` -- and every config layer
(user/project/local) is checked individually, so a real config can't ride in
under a test config that opts in. If no config file is picked up at all, there
is nothing to protect against and mngr runs normally. This makes the guard
secure by default: a real config (the developer's `~/.mngr` or the repo's
`.mngr/settings.toml`) loaded by a poorly-scoped test now trips the guard
instead of being used to perform real operations, while configs written
specifically for tests opt in explicitly.

The `MNGR_ALLOW_PYTEST=1` environment variable, which used to bypass the guard
entirely, has been removed. It had a single user, and the existence of such a
variable was not worth the risk of it being reached for as a quick bypass
instead of properly fixing a test with a leaky environment.

# Corrected `is_error_reporting_enabled` config field description

Separately, the `is_error_reporting_enabled` field description was out of date
(it described prompting to file GitHub issues); it now matches the actual
behavior -- suggesting a diagnostic agent on an unexpected interactive error.
