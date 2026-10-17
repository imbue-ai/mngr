mngr_modal: fix the session-end Modal app-leak detector, which could never report a leak.

`_get_leaked_modal_apps` (in `conftest.py`) read `"App ID"` / `"Description"` / `"State"` from `modal app list --json`, but the Modal CLI emits `app_id` / `description` / `state`. Every row therefore compared as empty and the detector always returned `[]`, so the `pytest_sessionfinish` check that is meant to fail a test session that leaked Modal apps never fired.

Both `_get_leaked_modal_apps` and `_get_leaked_modal_volumes` now parse through mngr's `utils/modal_cli.py`. If the CLI's output ever stops carrying the keys we read, `pytest_sessionfinish` reports that as a session failure rather than as "nothing leaked" (raising out of that hook would be silently dropped by pytest).
