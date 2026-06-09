Fixed the e2e tutorial connect tests (`test_connect.py`). The shared e2e
fixture wrote a `settings.local.toml` with a duplicate `type = "claude"` key
under `[commands.create]`, which made every `mngr create` in these tests fail
with a TOML parse error; removed the duplicate. The interactive
`run_connect_interactively` helper now clears the inherited `$TMUX`/`$TMUX_PANE`
and forces the builtin tmux attach (via `MNGR_CONNECT_COMMAND_ACTIVE`) so the
standalone `mngr connect` performs a real attach instead of being intercepted by
the no-op connect command or refused by the nested-tmux guard. The four
interactive connect tests also got a `@pytest.mark.timeout(120)` since the
create/attach/detach flow exceeds the default 10s per-test timeout. Test-only
change; no user-facing behavior changed.
