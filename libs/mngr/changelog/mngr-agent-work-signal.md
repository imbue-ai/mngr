Added: a `RUNNING_BACKGROUND` agent state, for an agent whose turn has ended while work it declared is still outstanding -- a background command, a subagent, a monitor or a workflow that is going to start its next turn without you. A standing schedule does not count: it is an appointment rather than work in flight, so an agent with one armed is idle until it fires.

Such an agent used to report `WAITING`, indistinguishable from one waiting on you. `RUNNING` and `WAITING` keep their meanings exactly: `RUNNING` is a turn in progress, and `WAITING` now means no turn is open and nothing is pending.

Whatever starts the work declares a *work lease* under the agent's `leases/` directory, whose first line names its own deadline as `until:<epoch seconds>`. Because each lease states a deadline, work that dies without cleaning up stops counting on its own, unlike the `active` marker, which strands when a hook fails to remove it.

`--running` now selects both states, since it asks which agents will move themselves and `RUNNING` alone stopped answering that. `mngr ls` reports the outstanding work as `leases`, so a caller can see which work is holding an agent rather than only that something is.

`resources/mngr_work_lease_lib.sh` ships the write side for shell hooks, so any agent plugin declares leases without reimplementing the format; it is provisioned into every agent alongside the other shared libraries. The probe reads them in the same host read that already collects the `active` marker, so the new state costs no extra round trip.

An agent that declares no leases behaves exactly as before.
