Fixed the `mngr event` tutorial e2e tests (`test_event.py`) so they run
reliably. Each test now carries an explicit `@pytest.mark.timeout(120)` (they
previously inherited the 10s default, which is too short for a release e2e test
that creates an agent and reads its events) and passes generous subprocess
timeouts to the `mngr create`/`mngr event` calls. Removed the superfluous
`@pytest.mark.modal` mark: these tests use the default (local) provider, which
never invokes Modal, so the resource guard rejected the unused mark. The tests
keep the `tmux` and `rsync` marks, which the local agent creation genuinely
exercises. No production behavior change.

Also strengthened `test_event_default` to verify the tutorial's documented
contract that `mngr event` emits clean JSONL: every line on stdout must parse
as a JSON object (catching warnings or log lines leaking into the jq-able
stream), and any events present must carry the four guaranteed fields
(`event_id`, `timestamp`, `source`, `type`).
