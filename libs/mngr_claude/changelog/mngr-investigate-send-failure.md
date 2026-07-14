# Claude: content-verified message delivery

- Changed: `mngr message` to a claude agent now confirms submission by finding the message's own content in claude's durable transcripts -- checking BOTH the native session JSONL (resolved from `$CLAUDE_CONFIG_DIR` and the `claude_session_id` marker at poll time) and mngr's raw copy (`logs/claude_transcript/events.jsonl`), so a dead transcript watcher or unexpected config-dir layout cannot alone cause a false delivery failure. This replaces waiting on the `mngr-submit-<session>` tmux `wait-for` signal, whose latch-on-unconsumed-signal semantics caused sends to report success for messages that were never submitted.

- Changed: messages whose normalized content is too short to carry identity (emoji-only, punctuation) confirm on any newly appended enqueue/user record instead; slash commands (`/clear`, `/compact`, ...) use a relaxed policy (session-id change, `active`-marker touch, or any new record counts; nothing is required and the send never hard-fails).

- Changed: existing agents (created by older mngr versions) are fully supported without reprovisioning -- every artifact the probes read already exists on them. Newly created agents still fire the (now-unlistened) `mngr-submit` hooks so older mngr senders keep confirming; the hooks are marked for removal in a future release.

- Added: a warning plus a structured agent event when Claude's input box already contains leftover text before a send (detected via the `❯` prompt row).

- Added: release tests -- a message-delivery journey (idle -> busy/queued -> rapid sequential -> long message -> `/clear`, each delivered exactly once) and a concurrent-delivery test with two claude agents on one tmux server.
