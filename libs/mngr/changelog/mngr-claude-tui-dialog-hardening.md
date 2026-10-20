`mngr message` now distinguishes three outcomes so a caller can tell them apart by exit code:

- `0`: the message was delivered and no blocking dialog remained.
- `7` (new): the message was delivered, but a blocking interactive dialog (e.g. Claude's `/model` confirmation) could not be resolved and the agent is now stuck on it. See the Claude agent's `auto_accept_prompt_depth` setting.
- any other non-zero: the message was not delivered.

Blocked agents are reported separately from failed ones in the human, JSON, and JSONL output. Internally this adds a `MessageDeliveredButBlockedError` and a `blocked_agents` list on the message result; interactive TUI agents gain a post-submit hook (a no-op by default) that runs after a send is confirmed, which the Claude plugin uses to detect and auto-accept (or surface) a dialog opened by the just-sent message.

Internal: the agent `wait_for_ready_signal` parameter `is_creating` was renamed to `is_readiness_awaited` (it gates whether the call waits for the agent's readiness signal -- the TUI ready indicator, or a launch/session sentinel). No behavior change.
