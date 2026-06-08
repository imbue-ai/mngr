Fixed the `test_event_tail` tutorial e2e test (`mngr event my-task --tail 20`).

- Added a `@pytest.mark.timeout(120)` override so the test is not killed by the
  default 10s per-test timeout while it creates a local command agent and reads
  its events.
- Removed the inapplicable `@pytest.mark.modal` mark: `mngr event <agent>`
  resolves the agent via the discovery event-stream optimization, which narrows
  discovery to the agent's (local) provider and never invokes Modal, so the
  resource guard flagged the mark as never exercised.
- Strengthened the assertions to verify the documented JSONL contract: every
  emitted line is a JSON object carrying the guaranteed `event_id`, `timestamp`,
  `source`, and `type` fields, and `--tail 20` yields at most 20 events.
