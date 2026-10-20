The interactive pi-coding agent now captures its own stderr to `stderr.log` in the agent's
state directory. Agents run under tmux rather than supervisord, so a startup error or
crash reached none of the workspace's service logs, and unlike codex, antigravity and
opencode this harness wrote no diagnostic file of its own -- a bug report had no way to
see it. The TUI still renders on stdout, so the pane is unchanged; the file is truncated
per launch, which bounds it without rotation. The bug-report collector picks it up with
the rest of the agent's logs.

Note the tradeoff: stderr now goes to the file instead of the pane, so a crash message
that used to stay in the pane's scrollback is no longer visible there. It is still
collected -- from the file, and durably, which the pane was not.
