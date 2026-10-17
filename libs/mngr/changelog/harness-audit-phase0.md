- Fixed quality-gate failures from the stop-button/message-lock work: the stub host used by the `press_key_chord` unit tests now declares `is_local` as a constructor parameter (resolving type errors from post-construction attribute assignment), and a trailing comment in those tests moved to its own line to satisfy the trailing-comment ratchet.

- Reformatted `interfaces/agent.py` with ruff (no behavior change).

- Made the interactive `handle_not_implemented_error` tests in `cli/issue_reporting_test.py` hermetic: they now pin `IS_AUTONOMOUS` (via `monkeypatch.delenv`) so the prompt-and-report path is exercised even in sandboxes that export `IS_AUTONOMOUS=1`.

- `test_install_script.py` now locates the repo root by walking up to the first ancestor containing `scripts/install.sh` (falling back to the nearest `.git` ancestor), so the install-script tests also pass when the mngr monorepo is vendored inside another git repository.
