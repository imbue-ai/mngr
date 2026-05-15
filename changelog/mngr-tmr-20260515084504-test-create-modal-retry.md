Improved the release test `test_create_modal_retry` to actually exercise the
`[retry]` settings.toml block from the tutorial: it now drops the documented
retry config into the project settings.toml and asserts that
`mngr config get retry.connect_retry_times` / `retry.connect_retry_delay`
return the configured values before running the modal create.
