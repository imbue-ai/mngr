TMR: when running against a remote provider with `--use-snapshot` (or
`--snapshot=<id>`), avoid re-uploading the code repo for every test agent.
The snapshotter agent's work_dir is now pinned to `/code` on its host, and
each test agent created from the resulting snapshot sources from that
on-host `/code` via `git-worktree` -- previously each agent re-pushed the
git history from the laptop.

TMR: when launching modal agents, override the modal provider config to
skip the per-agent "initial" filesystem snapshot. That snapshot adds 60-90s
per agent and runs once per agent (so 4 agents on a pooled host trigger
four snapshots), even though TMR's pooled hosts are ephemeral and the
snapshotter's host is snapshotted explicitly already.
