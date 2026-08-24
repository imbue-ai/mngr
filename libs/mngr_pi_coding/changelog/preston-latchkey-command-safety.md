The lifecycle extension no longer carries checks or task-tracking rules of its own choosing.

It used to spawn a fixed checker script before each bash tool call, and to run a task-tracker discipline -- a reminder when a tool ran with no step in progress, a carryover of still-open steps into the next turn's system prompt, and a note on settle -- reading state from a tracker binary in the agent's work dir. All of that belonged to the repo the agent runs in, not to the agent runner: it assumed that repo's tracker, its command names, and its prose.

What remains is what holds for any pi agent: the pipe-into-`tail`/`head` and git history-rewrite blocks, and the OOM/git-identity command rewrite.

pi auto-discovers extensions from `.pi/extensions/` in the project and composes handlers across extensions -- `tool_call` blocks when any returns `{block, reason}`, `tool_result` handlers chain like middleware, `before_agent_start` chains the system prompt -- so a repo ships its own extension there and it runs alongside this one.

Because they share one event, the command rewrite now also records the command the agent actually wrote, as `mngrOriginalCommand` on the tool-call event. A guard in another extension that reads `input.command` after the rewrite would otherwise see the OOM/git-identity prefix as a command chained ahead of the agent's, and refuse it; pi gives no control over handler order, so the pre-rewrite command travels with the event instead. This is the pi equivalent of running the rewriter last, which is how the same guards stay correct on claude and codex.
