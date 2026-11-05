Added: a Claude agent now declares the background work its turn leaves running, so mngr reports it as RUNNING_BACKGROUND until that work wakes it.

At each turn end `wait_for_stop_hook.sh` declares one work lease per in-flight background task under the agent's `leases/` directory, taken from the `background_tasks` the Stop payload carries for exactly this purpose. That set covers a background command, an armed monitor, a workflow and a backgrounded subagent alike, and a task reporting a finished status is skipped rather than leased. A `session_crons` schedule is deliberately not leased: a schedule is an appointment rather than work in flight, so an agent with one armed is idle until it fires.

The hook writes leases through the shared `mngr_work_lease_lib.sh` rather than spelling the format itself, and keeps only the job that is genuinely its own: reading the Stop payload. It rewrites its own leases at every turn end, so work that has finished stops counting, and leaves leases written by anything else alone, since each states a bound it expires by. Nothing refreshes a lease: its deadline bounds how long background work that died silently can hold an agent, so it only has to outlast the longest task that runs without waking the agent. An agent provisioned before that library existed declares no leases and still ends its turns normally.

A restart drops those leases outright, alongside the `active` and `permissions_waiting` markers the startup hook already cleared: the tasks were processes the restart killed, so an agent restarted mid-lease reads WAITING immediately rather than when the deadline passes.

Previously an agent that ended its turn with a subagent, a review or a monitor still running was indistinguishable from one waiting on you.
