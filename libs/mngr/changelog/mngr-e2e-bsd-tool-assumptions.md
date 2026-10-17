The `mngr exec --start` / `--no-start` tutorial examples now run `uname -s` instead of `cat /etc/os-release`. macOS has no `/etc/os-release`, so following the tutorial on a Mac produced `cat: /etc/os-release: No such file or directory` rather than the demonstrated output.

Three e2e tutorial tests that relied on GNU-only tool behavior now pass on macOS as well as Linux:

- `test_exec_with_start` and `test_exec_no_start` assert on the host's own kernel name from `uname -s` rather than on `/etc/os-release` fields.

- `test_create_with_env_vars` reads the two variables it sets with one `printenv` call each. BSD `printenv` accepts a single operand and silently ignores the rest, so `printenv DEBUG LOG_LEVEL` returned only `DEBUG` on macOS and the `LOG_LEVEL` assertion failed even though the variable had been set correctly.
