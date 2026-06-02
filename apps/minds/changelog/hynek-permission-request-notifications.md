Fixed the desktop app's live permission-request notifications, which previously never updated: the requests badge, the requests-panel auto-open, and the in-panel list only refreshed after the user manually closed and reopened the panel.

Two root causes:

- The chrome SSE stream keyed its change detection off the bare pending-request *count*. Because latchkey requests are deduplicated by `(agent_id, scope, request_type)`, re-requesting the same scope (or resolving one request while another arrives) keeps the count constant while the contents change, so no update was emitted. The stream now diffs a content-based payload (`count` plus the ordered list of pending `request_ids`) and emits whenever the pending *set* changes. The SSE event was renamed from `request_count` to `requests` to reflect that it carries the id list, not just a count.

- The Electron main-process SSE consumer (`runChromeSSELoop`) wedged permanently the first time the auth-cookie sync forced a reconnect: `req.abort()` does not emit a terminal event on Electron's `ClientRequest`, so the awaited connection promise never resolved and the live consumer died seconds after launch. The loop now resolves that promise directly on a forced reconnect (via a shared finish ref) instead of relying on `'abort'`/`'close'` events, the latter of which fired eagerly on healthy streaming responses and caused a reconnect storm that leaked backend SSE generators and exhausted the connection pool.

In the Electron consumer, the requests panel now refreshes whenever the pending id set changes (not only on a count increase), and auto-open triggers when a genuinely new request id appears (so approving/denying never reopens a panel the user closed).
