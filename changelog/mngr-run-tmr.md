- `mngr create -vv` now emits a `Transferring agent files` log span around the
  per-file `write_file` loop in agent provisioning, so the total time spent
  pushing plugin-declared files (e.g. Claude Code config) is visible in timing
  output.
- `mngr tmr` no longer crashes the whole orchestrator when a single agent
  fails its initial-message send (e.g. `SendMessageError` from the tmux
  paste-detection timeout). The launching loops now also catch `AgentError`
  alongside `MngrError` / `HostError`, log a warning, and continue with the
  remaining agents. This applies to test-agent launching (both batched and
  pre-launched modes) and to the integrator launch.
- Fix `mngr tmr` integrator launch (and any local-provider test-agent
  launch), which always failed with `Failed to generate a unique host name
  after 100 attempts`. The local provider has a single fixed host
  ("localhost"), so the new-host path can never find a free name; TMR now
  reuses the existing local host when the target provider is `local`,
  matching what `mngr create` already does.
- `mngr tmr` HTML reports now include rows for tests whose agent failed to
  launch (e.g. `SendMessageError` from a paste-detection timeout). They are
  rendered as errored entries instead of being silently dropped, and carry
  the actual agent name that was used for the failed launch attempt -- so
  the report row matches the host/tmux session if the user kept it for
  debugging. The `mngr create -vv` log span around `_execute_agent_file_transfers`
  now wraps the early-return path too, so the span is emitted (with
  `count=0`) even when the agent declared no file transfers.
- Stop the `claude plugin update` SessionStart hook from hanging Modal-launched
  agents at an `ssh` first-contact (TOFU) prompt for github.com. The plugin
  updater shells out to `git pull`, which uses `ssh` -- on a fresh sandbox
  with no `~/.ssh/known_hosts` entry, ssh blocks on a "Are you sure you
  want to continue connecting" prompt that Claude Code's bypass-permissions
  setting does not cover. `scripts/claude_update_plugin.sh` now prefixes
  the update with `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes'`, which writes the first-seen host key to known_hosts
  and exits non-interactively if anything goes wrong (matching the
  script's existing `2>/dev/null || true` failure tolerance).
- `mngr tmr` HTML reports now have a dedicated "Failed" section,
  separate from "Blocked". The two represent different failure modes:
  Blocked means the coding agent reported every change as BLOCKED
  (i.e. it considered the work too complex), while Failed means an
  infrastructure failure prevented the agent from producing a verdict
  (launch failed, agent timed out, agent details missing). Errored
  results that previously fell into "Blocked" now route to "Failed".
