# Robust message delivery: durable-evidence confirmation for `mngr message`

- Fixed: `mngr message` could report success for a message that was never submitted. tmux latches a `wait-for -S` signal fired with no waiter, so a stale signal on the `mngr-submit-<session>` channel (left by any prompt submission no sender was waiting on, e.g. a task-notification dequeue) instantly "confirmed" the next send -- whose EXIT trap then killed the still-pending backgrounded Enter keystroke. The message stayed in the input box while every layer reported success.

- Changed: submission confirmation no longer listens on tmux `wait-for` channels at all. A new submit-and-confirm engine in `tui_utils` runs ONE sequential remote script per send -- capture per-probe baselines, send Enter, poll agent-supplied durable evidence probes -- with no background jobs and no traps, so Enter is always sent before any confirmation check can run.

- Changed: `InteractiveTuiAgent` subclasses now implement `_build_submission_evidence_probes` (durable on-disk evidence) instead of `_send_enter_and_validate` (send-Enter strategies). The unused `send_enter_best_effort` and `send_enter_and_poll_for_cleared_indicator` strategies were removed; agents supplying no probes degrade to a best-effort Enter. The `enter_submission_timeout_seconds` field is now `confirmation_timeout_seconds` (default 90s).

- Added: bounded, pane-gated Enter retries (at ~3s/10s/30s into the confirmation window): Enter is re-sent only while the pane still shows the pasted text, and the message text itself is never re-pasted, so mngr's own retries can never duplicate a message.

- Added: messages starting with `/` (TUI slash commands such as `/clear`) are confirmed under a relaxed policy: same retries and a brief evidence poll, but the send succeeds even when no evidence is observable, logging a warning and recording a structured agent event (`events/messages/events.jsonl`) instead of failing.

- Added: unconfirmed strict sends fail with rich diagnostics (per-probe baseline/final tokens, Enter-retry history, pane capture). `mngr create --message` and resume-message failures now explain that the agent itself is up and how to resend (`mngr message <agent>`).

- Added: a warning plus a structured agent event when the input box already contains text before a send (the new message is appended, as before).

- Added: release-test harness journeys for message delivery (`run_message_delivery_journey`: idle -> busy/queued -> rapid sequential -> long buffer-pasted message -> slash command, each delivered exactly once) and concurrent delivery to two agents on one tmux server (`run_concurrent_message_delivery`).

- Fixed: the gevent-hub accumulation unit test (`thread_cleanup_test.py`) no longer fails spuriously on loaded machines -- its repeated full-heap `gc.collect()` passes could exceed the suite-default 10s timeout, so it now carries its own 60s timeout and a flaky retry.
