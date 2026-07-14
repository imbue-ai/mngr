Fixed the root causes of two flaky CI tests (issue #2456):

- Remote host connections now retry when the SSH handshake fails with paramiko's "Error reading SSH protocol banner" -- the signature of a freshly booted host (e.g. a new Modal sandbox or VPS) that accepts TCP before sshd is ready. Previously the first failed connect was fatal, surfacing to users as a spurious "Create agent failed" and making `test_snapshot_create_then_list_on_modal` flaky. Other connect failures (refused, unreachable, auth, host key) are still not retried, so genuinely-down hosts fail fast.

- Extras CLI tests no longer spawn the real `claude` CLI: a shared stub-`claude` fixture replaces the Node process (whose startup on a contended CI sandbox tripped the global 10s offload pytest-timeout in `test_extras_claude_plugin_subcommand` and `test_extras_no_args_shows_status`) while still exercising the real shell-out and JSON-parsing plumbing. The real `claude` probe stays covered by a new acceptance test with a Node-startup-sized timeout.

- Marked `test_worker_hubs_do_not_accumulate_across_polls` as known-flaky with a 30s per-test timeout: its 30 executor spinups and full-heap gevent-hub probes take ~5s even on an idle machine, so on a contended runner it could cross the suite-wide 10s pytest-timeout.
