# Desktop e2e opts FCT's config into the pytest guard (test-only)

mngr's `is_allowed_in_pytest` config field now defaults to `False`, and every
config loaded during a pytest run must opt in. The desktop-client Docker e2e
(`test_desktop_client_e2e.py`) deliberately loads forever-claude-template's real
`.mngr/settings.toml` (it pins `MNGR_ROOT_NAME=mngr` to get the create
templates), so it now adds `is_allowed_in_pytest = true` to that checkout for the
duration of the test and restores it afterward. The opt-in is intentionally
added in-test (not shipped in FCT's config, which would disable the guard for
every FCT-based project). Test-only change.
