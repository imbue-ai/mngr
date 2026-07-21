Hardened the `mngr rename --dry-run` e2e test against a flaky per-command timeout: each `mngr` invocation now gets a 90s timeout (the CLI's cold-start plugin-import cost can approach the 30s default on slow sandboxes), matching the existing convention in the multi-agent e2e test.

Also tightened the test to verify exactly its documented scope -- that the command previews the rename and does not apply it -- by removing an out-of-scope assertion that exec'd into the agent to check its process was still running.
