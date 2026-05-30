Fixed the `test_create_with_label` e2e release test (labels and host tags
from the tutorial).

- Removed the spurious `@pytest.mark.modal` mark. The test creates a purely
  local `--type command` agent and only runs `mngr list`, so it never invokes
  the Modal CLI binary. Because e2e tests run `mngr` as a subprocess (where the
  Modal SDK monkeypatch does not apply) and the resource guard only tracks the
  Modal CLI via its PATH wrapper, the mark triggered a spurious "Test marked
  with @pytest.mark.modal but never invoked modal" guard failure. This matches
  the documented rationale in `test_create_modal.py`, whose modal-marked tests
  use `--provider modal` (which does invoke the Modal CLI via `environment_create`).
- Added `@pytest.mark.timeout(120)`, matching the convention used by the other
  e2e create tests (`test_create_modal.py`, `test_create_commands.py`). The
  default 10s timeout is too short for an e2e agent creation (rsync transfer of
  the repo plus git worktree setup plus agent boot).
