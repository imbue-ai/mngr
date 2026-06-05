Improved test quality under `imbue/minds/cli`:

- `test_pool_create_derives_production_from_default_root_name` now drives the real
  `MINDS_ROOT_NAME=minds` -> `production` resolution via `require_activated_env_name()`
  instead of hardcoding the env name, so a regression in that mapping is actually caught.
- Consolidated the duplicated, divergent `_isolated_env` fixtures from `env_test.py` and
  `pool_test.py` into a single `isolated_activation_env` fixture in `conftest.py` that strips
  the superset of activation vars (`MINDS_ROOT_NAME`, `MODAL_PROFILE`, `MODAL_CONFIG_PATH`).
- Dropped redundant `HOME`/`chdir` setup that the autouse `isolate_mind_tests` already provides.
- `test_wipe_teardown_is_noop_without_profile_or_agents` now asserts the observable no-op
  (no "Destroyed ... mngr agent(s)" log line) via a loguru sink instead of only "did not raise".
- `test_validate_modal_profile_rejects_non_table_section` now pins the failure to the
  "no profile named ..." branch by asserting on the message text.
