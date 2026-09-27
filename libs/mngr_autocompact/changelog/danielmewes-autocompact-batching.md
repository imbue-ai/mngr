`mngr autocompact check` and `mngr autocompact run` now accept multiple agent targets (e.g. `mngr autocompact run agent-1 agent-2`). Targets resolve the same way as in other multi-target commands such as `mngr stop`: a name that matches agents on several hosts selects all of them, and an agent named more than once (for example by both name and ID) is compacted once.

Multiple targets are resolved with a single discovery pass, and each affected host's agent list is loaded once, in parallel across hosts. If any target is unknown, the command fails with one error that names every unknown target.

Host scanning for `--all` and the per-agent staleness checks and compaction requests now run in parallel.

The Python API `compact_all_agents` is replaced by `compact_stale_agents`, which also accepts an explicit list of agents to evaluate instead of discovering them.
