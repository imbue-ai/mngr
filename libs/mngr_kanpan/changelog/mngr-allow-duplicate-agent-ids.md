Board entries now carry the agent's `host_id`, and everything row-scoped is keyed by the (host, agent) instance: dired-style marks, focus/peek memory, and batch execution. Mark-driven commands (`mngr destroy`, `mngr connect`, mute) address the exact instance as `ID@HOST`, so acting on one row can never touch a same-id agent on another host (agent ids are unique per host, not globally).

Custom commands additionally receive `MNGR_HOST_ID`; `"$MNGR_AGENT_ID@$MNGR_HOST_ID"` is a full mngr address for the exact instance.

Known limitation (documented follow-up): data-source field maps remain keyed by bare agent id, so a column value may mix two same-id instances during a migration window (display-only).
