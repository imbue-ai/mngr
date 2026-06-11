Added shared agent-preservation wiring so any plugin can mirror the claude/usage preserve-on-destroy behavior with minimal code.

- `build_transcript_preserved_items(event_source)` returns the standard raw (`logs/<source>_transcript`) and common (`events/<source>/common_transcript`) transcript directories an agent writes, centralizing the on-disk convention.

- `preserve_agent_state(items, agent, host)` is a thin online-path wrapper (for a plugin's `on_destroy`) that resolves the agent's state directory and local preserved-files destination.

- `preserve_host_agents_on_destroy(host, mngr_ctx, agent_type, items_for_agent)` is the shared body for a plugin's `on_before_host_destroy` hookimpl: it skips hosts with no readable volume, filters discovered agents by `agent_type`, and preserves each opted-in agent straight off the host volume.
