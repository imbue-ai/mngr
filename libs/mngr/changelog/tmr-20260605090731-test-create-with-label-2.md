Fixed the `test_create_with_label` e2e tutorial test: gave it a 120s timeout
(it creates a real command agent and then runs `mngr list`, which exceeds the
default 10s budget) and removed the incorrect `@pytest.mark.modal` mark (the
test only creates a local `--type command` agent and never exercises Modal).

Added `test_create_with_invalid_label_format`, an unhappy-path test sharing the
same tutorial block, which verifies that `mngr create --label <no-equals>` is
rejected with a `KEY=VALUE` error and creates no agent.
