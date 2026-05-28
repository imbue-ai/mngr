# Offline agent field generators

Implemented the plugin hook previously documented as the planned `get_offline_agent_state`, now named `offline_agent_field_generators` to mirror the existing online `agent_field_generators` hook.

- Plugins can now contribute `plugin.<plugin_name>.<field>` data for agents whose host is offline or unreachable. Each generator receives the offline `(DiscoveredAgent, HostDetails)` (rather than the live `(agent, host)` the online hook gets) and computes fields from the cached `data.json` exposed via `DiscoveredAgent.certified_data`. `None` field values are omitted and empty plugins are dropped, exactly like the online path.
- `mngr list` collects these generators and threads them through `get_host_and_agent_details` to `build_agent_details_from_offline_ref`, so offline plugin fields are usable in `mngr list` columns and CEL filters just like online ones.
- Discovery snapshots now preserve plugin fields: `discovered_agent_from_agent_details` carries `AgentDetails.plugin` into the reconstructed `certified_data`, so offline generators can still read plugin state for fully-unreachable hosts that fall back to a persisted snapshot.
- Updated the plugins concept doc to document `offline_agent_field_generators` and remove the `[future]` `get_offline_agent_state` placeholder.
