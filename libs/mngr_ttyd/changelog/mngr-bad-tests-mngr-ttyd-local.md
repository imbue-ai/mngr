Strengthened the ttyd plugin test suite. Provisioning tests now run against a shared,
interface-backed fake host (`FakeTtydHost` in a new `testing.py`) that actually executes
commands and writes files, so they assert on real effects (the `commands/ttyd/` directory
is created, `agent.sh` lands on disk and is executable) instead of on recorded command
strings. The `ttyd_agent.sh` dispatch script is now verified by executing it against a fake
`tmux` to confirm session routing (named target vs. ambient session), and the embedded ttyd
and install shell programs are syntax-checked with `bash -n`. Redundant substring-grep
assertions over the command constants were removed in favor of these behavior tests. No
user-facing behavior change.
