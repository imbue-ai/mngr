Added shared agent-preservation wiring so any plugin can mirror the claude/usage preserve-on-destroy behavior with minimal code.

- `build_transcript_preserved_items(event_source)` returns the standard raw (`logs/<source>_transcript`) and common (`events/<source>/common_transcript`) transcript directories an agent writes, centralizing the on-disk convention.

- `preserve_agent_state(items, agent, host)` is a thin online-path wrapper (for a plugin's `on_destroy`) that resolves the agent's state directory and local preserved-files destination.

- `preserve_host_agents_on_destroy(host, mngr_ctx, agent_type, items_for_agent)` is the shared body for a plugin's `on_before_host_destroy` hookimpl: it skips hosts with no readable volume, filters discovered agents by `agent_type`, and preserves each opted-in agent straight off the host volume.

- `flag_gated_items(ref, flag_name, items)` is the shared offline selector helper: it returns `items` only when the discovered agent's persisted `agent_config[flag_name]` is truthy (else `None`), so plugins no longer hand-roll the same opt-in dict-walk for `on_before_host_destroy`.

- The shared agent release lifecycle (`run_agent_release_lifecycle`) now asserts preservation: its destroy step verifies the agent's raw and common transcripts actually landed in `<local_host_dir>/preserved/<agent-name>--<agent-id>/` (keyed on the seeded secret), so a swallowed preservation failure can no longer pass silently. Every plugin built on the shared lifecycle inherits the check.
