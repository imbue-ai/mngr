Made the `test_create_with_json_output` e2e tutorial test robust to unreachable
remote providers. Its verification step used an unscoped `mngr list --format json`,
which fans out to every configured provider (Modal, Docker, ...); when one is
unreachable (e.g. no Docker daemon), the default `--on-error abort` aborts the
whole listing and the test failed. The verification now scopes `mngr list` to
`--provider local` (the agent is created locally), matching the convention already
used by the other create tests (e.g. `test_agent_types`).
