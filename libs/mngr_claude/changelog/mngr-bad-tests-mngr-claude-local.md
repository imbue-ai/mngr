Test-quality hardening across the mngr_claude test suite (no user-visible behavior change). Replaced assertions that passed without verifying correctness with ones that check real effects:

- Seven `on_before_provisioning` tests that asserted nothing ("did not raise") now assert observable effects (config left untouched, missing-credentials warning present/absent, untrusted worktree rejected).
- `does-not-extend-trust` provisioning tests now assert the exact set of trusted projects instead of the presence of a key the test itself wrote.
- Transcript-converter truncation tests now assert exact lengths and the ellipsis marker (not just an upper bound), and the "skips event" tests now prove only the bad event is dropped (a known-good event survives) plus cover the missing-`timestamp` branch.
- Command-assembly and install-command tests now assert on shlex-parsed tokens / load-bearing flags instead of hand-rebuilt exact shell strings.
- Skill-install skip tests assert file content is unchanged instead of relying on mtime equality; added remote-install coverage. Custom-agent-type resolution test now sets a non-default field to prove it survives the merge.
- Grace-period headless test asserts the poll actually re-checked; removed dead `_patch_agent_as_stopped` calls and a fragile wall-clock timing assertion.
- claude_config no-op tests assert content is byte-for-byte unchanged; effort-callout check test isolated to the effort dialog.
- Removed an introduced `unittest.mock.patch` of the function under test in favor of the real no-credentials environment, and a duplicate local `temp_source_dir` fixture (now inherited from the shared modal conftest).
- Release/acceptance tests: the Modal provisioning test destroys the agent and asserts a non-empty preserved session JSONL; the adopt-session and modal tests drop brittle "Done." log-string checks; the no-dialog send_message test matches the specific downstream timeout; the background-tasks prefix-collision test asserts the script reached its gone-session exit; magic `sleep` literals replaced with a named constant.
