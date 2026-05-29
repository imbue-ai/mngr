Fixed the release e2e test `test_create_with_connect_command` (in `imbue/mngr/e2e/tutorial/test_create_commands.py`):

- Added `@pytest.mark.timeout(120)` so the create+connect+list flow (which includes the one-time ttyd install attempt) is not killed by the default 10s func-only timeout.
- Removed the superfluous `@pytest.mark.modal`. The test uses the default local provider and never invokes the `modal` CLI binary (the resource guard tracks the binary; `mngr list` reaches Modal via the SDK, not the CLI), so the mark tripped the guard's "marked modal but never invoked modal" check once the test was able to finish.
