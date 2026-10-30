- Removed the unmaintained full-lifecycle Electron harness, `apps/minds/scripts/electron_full_flow_e2e.py` (formerly run by `just minds-test-electron-flow`). It had not got past its create step since the create form stopped rendering `#configure-toggle` on 2026-06-24.

- Removed the harness-only helpers from `desktop_client/e2e_workspace_runner.py`: `run_full_workspace_flow`, `destroy_agent_best_effort`, `drive_create_docker_imbue_workspace` and the per-step helpers. Also removed `start_new_chat_from_launcher` and `open_terminal_from_launcher`, whose only remaining callers were their own unit tests, along with those tests. The remaining chat helpers save their debug screenshots to `/tmp/minds-electron-e2e/` rather than `/tmp/minds-electron-flow/`. `create_workspace_via_electron` and `drive_create_flow` drop their unused `account_label` parameter, which selected an `#account_id` field the create form no longer has.

- Coverage the harness was meant to provide, and that no surviving suite has, is tracked in MIND-637 (stopping and starting a workspace), MIND-638 (a terminal from the launcher) and MIND-639 (a second chat from the launcher).

- The minds `test_prevent_async_await` ratchet drops from 13 to 11. Its two matches were JavaScript `async`/`await` inside a deleted `page.evaluate` string.

- `docs/testing-overview.md` and `docs/dwt-mngr-pin.md` no longer describe the removed harness or its `destroy_agent_best_effort` helper; the e2e backlog in `testing-overview.md` now names the surviving drivers of the settings-page destroy and points at MIND-637, MIND-638 and MIND-639 for the stop/start, terminal and second-chat coverage.
