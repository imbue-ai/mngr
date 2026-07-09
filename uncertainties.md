# Uncertainties

Conflicts between documentation and observed behavior, recorded for later resolution.

## `mngr message` delivery signals (2026-07-09)

Observed on macOS with Claude Code 2.1.205 against a real local Claude agent. See
[specs/mngr-message-delivery-signals.md](specs/mngr-message-delivery-signals.md).

1. `libs/mngr_claude/imbue/mngr_claude/claude_config.py:617-619` states that without the
   `SessionStart` hook, `mngr message agent -m /clear` "would time out at
   `enter_submission_timeout_seconds` even though /clear actually executed", implying that
   with the hook it succeeds. Measured: `/clear` times out anyway (exit 1 after 93.7s).
   The hook fires `tmux wait-for -S`, but no waiter is ever registered because
   `timeout` is absent on macOS, so the signal is lost.

   Assumed for the spec: the comment describes the intended behavior on a host with GNU
   coreutils, and is silently wrong everywhere else.

2. `libs/mngr_claude/imbue/mngr_claude/plugin.py:2206` (`_build_accept_marker_command`)
   documents the `enqueue` marker as confirming submission "the moment the message is
   accepted rather than waiting on the (possibly slow) UserPromptSubmit hook". Measured:
   Claude only writes a `queue-operation` when a message must wait in a queue, so on an
   idle agent the marker never fires at all.

   Assumed for the spec: the docstring describes the busy-agent case and omits that the
   marker is inert when the agent is idle.
