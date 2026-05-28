# Dropped the removed `MNGR_ALLOW_PYTEST` from the env-settings spec

`MNGR_ALLOW_PYTEST` was removed from mngr in this PR (the pytest config guard is
now per-config via `is_allowed_in_pytest`). Removed the now-stale reference to it
from `specs/env-settings-overrides/concise.md`.
