Fixed the tutorial-tied `mngr event` e2e tests (`libs/mngr/imbue/mngr/e2e/tutorial/test_event.py`):

- Added `@pytest.mark.timeout(120)` to each test so the agent-creating commands are not killed by the global 10s pytest timeout.
- Removed the inappropriate `@pytest.mark.modal` marker: these tests create a local command agent and read its events without ever invoking Modal, so the resource guard correctly flagged the mark as superfluous.
- Added an unhappy-path test (`test_event_missing_agent_fails`) covering the same tutorial block: `mngr event` against a nonexistent agent fails with a clear "Could not find agent" error instead of silently printing an empty event stream.
