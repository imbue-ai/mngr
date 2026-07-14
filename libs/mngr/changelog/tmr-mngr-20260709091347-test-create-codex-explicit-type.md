Fixed the `test_create_codex_explicit_type` e2e tutorial test so it no longer
requires a codex binary (or npm) on the host: it now disables the codex install
check before creating the agent, so `--type codex` resolves and the agent is
created without attempting `npm i -g @openai/codex`. Also dropped the stale
`@pytest.mark.rsync` mark and the `--no-ensure-clean` flag (which had been the
only thing triggering rsync), matching the sibling command/yolo tutorial tests.
