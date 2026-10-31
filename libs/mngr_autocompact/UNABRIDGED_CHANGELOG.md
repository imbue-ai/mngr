# Unabridged Changelog - mngr_autocompact

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_autocompact/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-14

- Added `compact_stale_agents_by_name(mngr_ctx, names, now=None)` in `imbue.mngr_autocompact.manager`, the entry point for code that embeds mngr (such as the minds chat app's periodic sweep). It compacts whichever of the named agents are stale and returns the names it compacted.

- Unlike `mngr autocompact run <targets>`, it never fails on a name it cannot use: a name that matches no agent, an agent on an offline or unreachable host, a stopped agent, and an agent without compaction support are each skipped with a debug log, and a host that fails to load does not stop compaction on the others.

- `get_compaction_agents` accepts an optional `names` filter, and the autocompact discovery path logs at debug level why it skips an offline host, an agent missing from its host, an agent without compaction support, or a stopped agent.

- The "does not report context tokens; skipping compaction" message is logged at debug level rather than as a warning. A Claude chat has no context token count until its first turn completes, which is a normal state, and the minds chat app checks every opted-in chat each minute.

- The staleness check behind `compact_stale_agents_by_name`, `mngr autocompact check` and `mngr autocompact run` runs its cheap checks first: idle epoch, cache TTL, idle long enough, then context tokens. Whether the agent is running is checked last, only for an agent that passes everything else. That check spawns `tmux` and `ps` on the host, so a sweep spawns them only for agents that pass every other check. The same agents are compacted as when the running check comes first.

- `compact_stale_agents_by_name` and the other discovery-driven autocompact paths build only the requested agents with `load_agents_from_refs`, from the `data.json` contents discovery already read, rather than every agent left on each host (stopped agents and finished workers included). A corrupt record of an agent that was not requested does not fail the sweep.

- A pass whose per-host or per-agent workers are still unfinished after 300 seconds logs one warning naming them (for example "agent foo, host bar") and keeps waiting for them.

- The README documents `compact_stale_agents_by_name`, and says that `get_context_tokens()` reports `None` after a compaction until the agent's next turn, which is how the Claude and Pi harnesses avoid being compacted a second time straight after a compaction.

- A compaction never waits long on a busy agent. It waits at most 5 seconds (`COMPACTION_MESSAGE_LOCK_TIMEOUT_SECONDS`) for the agent's message lock; if another send (a user's message, a `mngr message`, a stuck send) still holds it, the agent is skipped until a later pass with a debug log, so an agent whose lock another send holds, even a stuck one, no longer keeps a pass of `mngr autocompact run` or `compact_stale_agents` from finishing, which blocked every later pass.

- Once it holds the lock, a compaction checks that the agent is still idle since the moment the staleness decision was based on. An agent the user messaged just as the check fired is skipped, with a debug log, instead of having `/compact` pasted into its new turn. Neither skip records a compaction, so the agent is reconsidered on the next pass.

- New `get_stale_idle_since`, which returns when a stale agent became idle (None when it is not stale); `is_agent_stale_for_compaction` is built on it. `trigger_compaction` accepts an optional `expected_idle_since`, and `compact_agent_if_stale` passes it the idle start it decided on.

- The README says that a check never waits on a busy agent, and documents the new `request_compaction` arguments.

Each staleness sweep evicts the cached transcript scans of agents it does not evaluate, keeping mngr's shared compaction transcript cache bounded to the agents still being checked.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-28

Introduce a DEFAULT_AUTOCOMPACT_MIN_CONTEXT_TOKENS named constant for the min autocompaction token threshold, and derived _INCREASED_AUTOCOMPACT_MIN_CONTEXT_TOKENS constants in the unit tests.

When running `mngr autocompact check` or `mngr autocompact run` with agent targets that do not support context compaction, log a warning and ignore those agents instead of failing with an error. Autocompaction continues for any targets that support it and exits with code 0.

## 2026-09-27

`mngr autocompact check` and `mngr autocompact run` now accept multiple agent targets (e.g. `mngr autocompact run agent-1 agent-2`). Targets resolve the same way as in other multi-target commands such as `mngr stop`: a name that matches agents on several hosts selects all of them, and an agent named more than once (for example by both name and ID) is compacted once.

Multiple targets are resolved with a single discovery pass, and each affected host's agent list is loaded once, in parallel across hosts. If any target is unknown, the command fails with one error that names every unknown target.

Host scanning for `--all` and the per-agent staleness checks and compaction requests now run in parallel.

The Python API `compact_all_agents` is replaced by `compact_stale_agents`, which also accepts an explicit list of agents to evaluate instead of discovering them.

Raise the default `min_context_tokens` for autocompact from 100,000 to 200,000, so agents with smaller contexts are no longer compacted by default.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-16

Fix `ContextCompactionMode` handling so `mngr autocompact check` and `mngr autocompact run` only consider agents stale if their mode is set to `PROACTIVE_TIMER`, require `expected_mode` in compaction staleness and execution APIs, and rename `compact_agent` to `compact_agent_if_stale`.

## 2026-09-14

Add `mngr_autocompact` plugin providing stateless staleness checking, on-prompt compaction hooks, and the `mngr autocompact` command group (`check` to inspect agents needing compaction and `run` to execute compaction) for conversational agents when token usage exceeds configured thresholds or caches become stale.
