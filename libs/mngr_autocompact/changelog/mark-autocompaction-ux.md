- Added `compact_stale_agents_by_name(mngr_ctx, names, now=None)` in `imbue.mngr_autocompact.manager`, the entry point for code that embeds mngr (such as the minds chat app's periodic sweep). It compacts whichever of the named agents are stale and returns the names it compacted.

- Unlike `mngr autocompact run <targets>`, it never fails on a name it cannot use: a name that matches no agent, an agent on an offline or unreachable host, a stopped agent, and an agent without compaction support are each skipped with a debug log, and a host that fails to load does not stop compaction on the others.

- `get_compaction_agents` accepts an optional `names` filter, and the autocompact discovery path now logs at debug level why it skipped an offline host, an agent missing from its host, an agent without compaction support, or a stopped agent.

- The "does not report context tokens; skipping compaction" message is now logged at debug level instead of warning. A Claude chat has no context token count until its first turn completes, which is a normal state, and the minds chat app checks every opted-in chat each minute, so the warning repeated once a minute for every new chat.

- The staleness check behind `compact_stale_agents_by_name`, `mngr autocompact check` and `mngr autocompact run` now runs its cheap checks first: idle epoch, cache TTL, idle long enough, then context tokens. Whether the agent is running is checked last, only for an agent that passes everything else. That check spawns `tmux` and `ps` on the host, so a sweep over mostly-ineligible agents no longer spawns two or three processes per agent per tick. Which agents are compacted is unchanged.

- `compact_stale_agents_by_name` and the other discovery-driven autocompact paths now build only the requested agents with `load_agents_from_refs`, from the `data.json` contents discovery already read, instead of reading and building every agent ever left on each host (stopped agents and finished workers included) on every sweep. A corrupt record of an agent that was not requested no longer fails the sweep.

- A pass whose per-host or per-agent workers are still unfinished after 300 seconds logs one warning naming them (for example "agent foo, host bar") and keeps waiting for them.

- The README documents `compact_stale_agents_by_name`, and says that `get_context_tokens()` reports `None` after a compaction until the agent's next turn, which is how the Claude and Pi harnesses avoid being compacted a second time straight after a compaction.
