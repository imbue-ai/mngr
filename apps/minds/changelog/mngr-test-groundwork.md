# Extract Electron e2e workspace creation flow into a reusable runner

Split the Playwright-over-CDP driver out of
`apps/minds/test_desktop_client_e2e.py` into a new module at
`apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py` so the
same flow can be invoked outside pytest. The new module exposes the
public entry points `create_workspace_via_electron`, `resolve_fct_path`,
`ensure_minds_env_defaults`, `configure_logging`, `find_free_port`, and
`destroy_agent_best_effort`; everything else stays underscore-prefixed.

The existing pytest test was reduced to a thin wrapper that:

- calls `ensure_minds_env_defaults(setenv=monkeypatch.setenv)` so any
  injected env vars get reverted between tests,
- delegates the actual Electron / Playwright flow to
  `create_workspace_via_electron`, and
- always calls `destroy_agent_best_effort` in `finally` so a successful
  test never leaks an agent into the host.

`scripts/snapshot_minds_e2e_state.py` is the second caller: it invokes
`create_workspace_via_electron` directly and deliberately omits the
`mngr destroy` cleanup, because the whole point of the snapshot is to
capture a sandbox in which the workspace's Docker container is alive.

Also added a `*/desktop_client/e2e_workspace_runner.py` exclusion to the
`test_prevent_direct_subprocess` ratchet, since the new module
necessarily shells out to `electron`, `git`, and `uv run mngr destroy`
(operator-tool subprocesses with no `ConcurrencyGroup`-managed
equivalent). No user-visible behavior change.
