Agent listings from this provider now report the branch an agent's work_dir is
actually on.

mngr broadened `AgentDetails.initial_branch`: it used to name only a branch mngr
created, and is now the branch the work_dir was placed on either way, including
one that already existed and was merely checked out. This provider builds
`AgentDetails` by hand from the agent's `data.json` in its bulk-listing path
rather than going through the shared builders, so without this change its online
listing would have kept reporting the old narrower value while its offline path
reported the new one -- the answer would have changed with host state.

It reads through the shared `read_checked_out_branch` helper, so the fallback for
records written before the underlying field existed is applied identically
everywhere rather than restated per provider.
