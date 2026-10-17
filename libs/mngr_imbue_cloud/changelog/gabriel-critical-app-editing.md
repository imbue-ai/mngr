The lease-adopt `create_agent_state` override now carries the agent's
checked-out branch through alongside the created one, and this provider's
listings report it.

mngr broadened `AgentDetails.initial_branch`: it used to name only a branch mngr
created, and is now the branch the work_dir was placed on either way, including
one that already existed and was merely checked out. That is backed by a new
`checked_out_branch_name` in the agent's `data.json`. Because this provider adopts
a pre-baked agent's `data.json` and patches only selected fields in place rather
than writing a fresh one, it has to patch the new field too -- otherwise an
imbue_cloud agent would report no branch at all while every other provider
reported one.

It follows the same caller-supplied-wins rule as `created_branch_name`: patched
only when the caller passes a value, so the bake's own value stays intact when an
external mngr CLI user drives the lease flow without naming a branch.

The bulk-listing path, which builds `AgentDetails` by hand from `data.json`, reads
the field through the shared `read_checked_out_branch` helper, so the fallback for
records written before it existed is applied identically everywhere.
