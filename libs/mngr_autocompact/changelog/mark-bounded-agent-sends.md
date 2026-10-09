- A compaction no longer waits on a busy agent. It waits at most 5 seconds (`COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS`) for the agent's message lock; if another send (a user's message, a `mngr message`, a stuck send) still holds it, the agent is skipped until a later pass with a debug log. Before, one held lock or one wedged tmux server blocked `mngr autocompact run` and `compact_stale_agents` until the other send finished, which in a long-lived embedder stopped idle compaction for every agent.

- Once it holds the lock, a compaction checks that the agent is still idle since the moment the staleness decision was based on. An agent the user messaged just as the check fired is skipped, with a debug log, instead of having `/compact` pasted into its new turn. Neither skip records a compaction, so the agent is reconsidered on the next pass.

- New `get_stale_idle_since`, which returns when a stale agent became idle (None when it is not stale); `is_agent_stale_for_compaction` is built on it and gives the same answers. `trigger_compaction` accepts an optional `expected_idle_since`, and `compact_agent_if_stale` passes it the idle start it decided on.

- The README says that a check never waits on a busy agent, and documents the new `request_compaction` arguments.
