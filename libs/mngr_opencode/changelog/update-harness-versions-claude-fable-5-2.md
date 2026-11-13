The real-CLI release tests run on OpenCode Zen's `deepseek-v4.1-flash-free` (was `deepseek-v4-flash-free`). Zen retired the old free model, which now fails every request with a server error, so the agent never replied.

`test_opencode_waiting_reason_reports_permissions` can pass again. Its profile inherited the lifecycle test's `auto_allow_permissions`, whose wildcard allow replaces the whole permission block, so the `bash: ask` policy it relies on never reached opencode and no approval prompt could ever appear.

Both release tests are marked flaky. `mngr create` intermittently fails with "OpenCode did not signal readiness within 10s": loading mngr's two opencode plugins makes the first session take 4-6s to create (0.15s without them), which leaves little of the 10s budget once the server's own startup is counted.
