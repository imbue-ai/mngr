# Stopped workspaces never listed from this install are discovered as workspaces

Discovery surfaces non-running Imbue Cloud workspaces from the connector's lifecycle listing, re-attaching the agents cached from an earlier running listing. When no such cache existed on this install (the workspace was never seen running here), the synthesized stand-in agent carried no labels, so every consumer that recognizes a workspace by its `is_primary` label -- the minds workspace list and the `mngr forward` agent filter -- dropped the stopped workspace entirely.

The stand-in now carries the pre-baked services agent's name (`system-services`) and its `is_primary` label, which the pool row's `agent_id` identifies by construction, so a stopped workspace is listed as a workspace (with its STOPPED state and Start control) from any install.
