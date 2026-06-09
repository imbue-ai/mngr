Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"`
key under `[commands.create]` in `settings.local.toml`, making the file
unparseable and breaking every e2e tutorial test that depended on it. Added a
120s function-timeout override to `test_create_with_no_ensure_clean` (a real
create plus `mngr list` exceeds the default 10s), and added an unhappy-path test
verifying that `mngr create` aborts on a dirty working tree when
`--no-ensure-clean` is omitted.
