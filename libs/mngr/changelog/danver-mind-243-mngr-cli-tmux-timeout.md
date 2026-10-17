Creating an agent on a local host no longer shells out for work the local filesystem can do
directly. `write_file` used to write the bytes in-process and then spawn `sh -c chmod ...` to
apply the mode, and spawn `sh -c mv ...` for an atomic write; on the local path both are now
`Path.chmod` and `os.replace`. Remote hosts are unchanged and still use the shell. A local
`mngr create` was paying nine chmod spawns for scripts it had already written.

`load_config` also resolved the current directory's git worktree root twice per invocation --
once to locate the project config directory and once for `MngrContext.project_root` -- running
`git rev-parse --show-toplevel` as a subprocess each time. It now resolves it once and derives
both. `resolve_project_config_dir` is split into the override lookup, the precedence rule
(`derive_project_config_dir`, over an already-resolved root), and the resolve-then-derive path
for callers that have no root in hand; the git lookup is still skipped entirely when
`MNGR_PROJECT_CONFIG_DIR` already settles the answer.

Together these cut a real-agent CLI test from 28 subprocess spawns to 17. Process-spawn
latency is what stretches under a loaded CI runner, so this is also why those tests were
tripping the global 10s pytest-timeout.

Tests marked `@pytest.mark.tmux` now inherit a 60s budget from the shared conftest hooks
rather than the repo-wide 10s default, so the 53 hand-applied `@pytest.mark.timeout`
decorators at or below that value have been removed along with the comments that justified
them. Decorators above 60s stay: those tests are genuinely slower than their class.

A regression test now pins the budget itself: it asserts, from inside a live pytest session,
that a tmux-marked test with no timeout of its own resolves to a budget large enough for real
tmux work, and that a test in no such class is still left on the global default. Against the
previous behaviour it fails with "resolved to a 10.0s budget".
