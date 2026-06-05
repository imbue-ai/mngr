Added `**/tmr-report/` to the root `.gitignore` so the test-orchestrator
(mapreduce) run-report directory written into a worktree is not flagged as an
untracked change. The existing `**/tmr_*/` pattern did not match the
dash-separated `tmr-report/` name.
