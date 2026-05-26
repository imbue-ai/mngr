## mngr

- Test fix: `test_env_var_mngr_headless` no longer times out under the 10s default pytest timeout (the `mngr list` call triggers slow Modal provider discovery). Added `@pytest.mark.timeout(60)` and removed the incorrect `@pytest.mark.modal` mark (the test only runs `mngr list` and `mngr config get`, neither of which invokes the `modal` CLI binary that the resource guard tracks).
