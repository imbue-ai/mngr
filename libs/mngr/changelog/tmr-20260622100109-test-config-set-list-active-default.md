Fixed the `test_config_set_list_active_default` release test (tutorial coverage for `mngr config set commands.list.active true`):

- Added a `@pytest.mark.timeout(60)` override so the test is not killed by the default 10s per-test timeout while `mngr list` runs the full provider-discovery path.

- Removed the inaccurate `@pytest.mark.modal` mark: in a fresh, empty environment `mngr list` skips the Modal provider (the Modal environment does not exist yet) and never invokes the `modal` CLI, so the mark tripped the resource guard's "marked but never invoked" check (matching `test_list_active_filter` / `test_list_stopped_filter`).

- Verify the written default by reading the project `settings.toml` with `cat` (seeding the pytest opt-in up front so the follow-up `mngr list` can load the file), instead of a follow-up `mngr config get` that would be rejected by the pytest config-opt-in guard.

- The shared e2e fixture now disables the credential-backed cloud providers (AWS, Azure, GCP, OVH, Vultr) in its seeded local config. The dev/test image installs every provider backend via `uv sync --all-packages`, so they were probed by default and made `mngr list` exit non-zero in environments without their credentials; real users only install the provider extras they use. Modal, Docker, and local discovery are unaffected.
