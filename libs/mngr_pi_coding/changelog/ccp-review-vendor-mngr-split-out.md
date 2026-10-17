The pi lifecycle extension now records the live model and thinking level (pi's effort
axis) to `$MNGR_AGENT_STATE_DIR/pi_model_state.json` (`{provider, model, thinking_level}`).
It writes on `session_start` -- which fires at TUI startup, before the first prompt, so
the pre-turn-1 selection is available immediately -- and refreshes on `model_select` /
`thinking_level_select` as the user switches. This gives the chat model bar a low-latency,
on-disk source for pi's current model and effort, which pi otherwise exposes only through
its extension API.

Messages sent to a running pi agent are now delivered as `steer` rather than `followUp`.
pi's agent loop re-polls its steering queue after every tool-call round and injects steered
messages before the next model response, so a message sent mid-run reaches the agent greedily
at the next tool boundary instead of waiting for the whole turn to end. Delivery to an idle
agent is unchanged (it starts a turn either way).

The lifecycle extension's model-state mirror moves to the harness-uniform contract: it now
writes `$MNGR_AGENT_STATE_DIR/model_state.json` with the shared `{model: "provider/id",
effort, fast}` schema (previously `pi_model_state.json` with pi-specific keys), atomically via
tmp + rename. The system interface reads the same file name and schema for every harness.

The model-switch control channel becomes a single-slot mailbox (`pi_control.json`): the model
bar's resolver atomically overwrites it with the newest intent (an unconsumed older pick is
replaced -- last wins), and the extension consumes it (rename, apply, delete) at session start
and on its poll. A switch made while the agent is stopped now applies on the next start instead
of being silently swallowed by the old append-log's startup baseline.

The pi lifecycle extension now supports an atomic shoulder-tap. Minds appends a
`{"minds_interrupt": true}` control line to `pi_inbox`; on it the extension, in one
synchronous tick, interrupts the running turn (only if one is running) and captures the
steers pi drains into the composer -- clearing and restoring any user draft around the
drain so the resubmit is sourced purely from pi's own queue -- then, once the turn
settles, resubmits them as one merged turn. Injection is paused between the interrupt and
the resubmit so nothing opens a competing turn. Delivery to an idle agent is a no-op.

The lifecycle extension now generation-scopes the durable inbox at load: any `pi_inbox`
lines present when the extension loads belong to a prior process generation, so they are
archived verbatim to a sibling `pi_inbox_history` (raw history preserved) and `pi_inbox`
is truncated in place before the injection offset is seeded. The inbox therefore only
ever holds current-generation lines, so the Minds queue mirror's replay of `pi_inbox`
from zero can no longer resurrect dead generations' messages as phantom queued entries.
Safe against races: mngr appends to the inbox only after the readiness sentinel, which
`session_start` writes after load. Behavior toward pi is unchanged -- pre-existing lines
were already never re-injected (the offset seed skipped them).

Pi agents now enforce the same shell-command policy guards claude/codex do. The lifecycle
extension gains a `tool_call` handler that, for the bash tool, blocks a command piping into
`tail`/`head` and the git history-rewriting commands (`rebase`, `commit --amend|--fixup`,
`pull --rebase`) by returning `{block, reason}`, and otherwise rewrites the command in place with
the OOM self-tag + the agent's git identity (name from the agent's `data.json`, email
`<agent_id>@<host_id>`). Pi has no shell-hook surface, so the same rules the claude/codex scripts
apply are re-expressed in ~20 lines of TypeScript; the handler fails closed. Loaded only via the
per-agent `pi -e` flag, so a user's normal `pi` is unaffected. See `system/scripts/POLICY_HOOKS.md`.

The lifecycle extension now handles a second inbox sentinel for the stop button:
`{"minds_interrupt_retract": true}` is the retract sibling of the shipped
`{"minds_interrupt": true}` flush. On it the extension interrupts the running turn using the
same shared abort-and-capture core (interrupt only when a turn runs, clear and restore the
user's draft around pi's own steer drain) but DISCARDS the captured steers instead of
resubmitting them -- Minds hands the queued messages back to the user's composer, so
resubmitting here would double-deliver. A distinct key (not a field on the flush sentinel)
means an older extension treats it as inert rather than mistaking a retract for a flush. Both
sentinels are now tick-deferred: a sentinel is never consumed in a drain tick that already
injected a string line (the async send must park the steer first), so the steer is always
flushable/retractable before the abort. Delivery to an idle agent is a no-op, and a
retract discards nothing then keeps draining so later messages still inject.

The inbox sentinel (flush and retract) now waits for every injected steer to actually PARK
before it aborts, not just for one drain tick to elapse. The extension tracks the count of
in-flight `sendUserMessage` injections (settled in a `finally`) and defers a sentinel while any
remain outstanding, in addition to the existing same-tick deferral. Previously the sentinel was
deferred exactly one poll on the assumption the async send parked within it; a slower-parking
steer could be aborted before it landed and then escape the flush/retract to commit as a stray
turn. Gating on the actual settle makes "the steer has parked before the abort" hold regardless
of send latency (the stop button's in-flight message is provably captured-and-retracted, never
left to run). Idle delivery is still a no-op.

The pi lifecycle extension now enforces the tk workflow-discipline guards, matching claude/codex: a
non-standalone `tk start`/`close` is blocked in the `tool_call` handler (reusing the shared
`claude_tk_standalone_check.py`); a require-steps reminder is appended to the `tool_result` content when
a substantive tool ran with no in-progress step; an open-steps carryover reminder is appended to the
turn's system prompt in `before_agent_start`; and an open-steps stop nudge is written to stderr on
`agent_settled`. Step state is read from the vendored `ticket` binary (invoked via `bash`, so it works
on a noexec mount). See `system/scripts/POLICY_HOOKS.md`.

The lifecycle extension now writes `model_state.json` instead of `minds_model_state.json`, matching the rename on the claude side: the plugin has no reason to be named for one of its clients.
