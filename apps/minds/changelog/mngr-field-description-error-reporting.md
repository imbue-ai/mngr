# Test-infra comment update for the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False` and the
`MNGR_ALLOW_PYTEST` escape hatch was removed. No behavior change here: the
`clean_env` helper's docstring was updated to explain that minds subprocess
tests pass the pytest guard because they inherit `MNGR_HOST_DIR` pointing at the
shared-fixture-seeded tmp profile (which sets `is_allowed_in_pytest = true`).
