# Corrected `is_error_reporting_enabled` config field description

The description for the `is_error_reporting_enabled` config field was out of
date: it claimed the option controls prompting users to report unexpected
errors as GitHub issues. The option actually controls whether, on an unexpected
error while running interactively, mngr suggests launching a diagnostic agent
via a copy-paste-ready `mngr create` command. The description now matches that
behavior.

# `is_allowed_in_pytest` now defaults to False, and the `MNGR_ALLOW_PYTEST` escape hatch is gone

The `is_allowed_in_pytest` config field now defaults to `False` (previously
`True`). During a pytest run, `load_config` now refuses to run when a config
file is actually loaded that does not set `is_allowed_in_pytest = true`. If no
config file is picked up at all, there is nothing to protect against and mngr
runs normally. This makes the guard secure by default: a real config (the
developer's `~/.mngr` or the repo's `.mngr/settings.toml`) loaded by a
poorly-scoped test now trips the guard instead of being used to perform real
operations, while configs written specifically for tests opt in explicitly.

The `MNGR_ALLOW_PYTEST=1` environment variable, which used to bypass the guard
entirely, has been removed. It had a single user, and the existence of such a
variable was not worth the risk of it being reached for as a quick bypass
instead of properly fixing a test with a leaky environment. The field
description was updated accordingly.
