# Test-infra comment update for the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False` and the
`MNGR_ALLOW_PYTEST` escape hatch was removed. No behavior change here: the
`build_subprocess_env` helper's docstring was updated to explain that subprocess
tests pass the pytest guard because the shared fixtures seed the isolated tmp
profile with `is_allowed_in_pytest = true`, not because no config is loaded.
