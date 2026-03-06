# Design: Remove mng list --json fallback by using HOST_SSH_INFO discovery events

## Context

The changelings forwarding server's `MngStreamManager` previously used `mng list --stream` for agent discovery but fell back to `mng list --json` to get SSH info (needed for tunneling to remote agents). The goal was to add SSH info to the discovery event system so the forwarding server can get everything from the stream alone.

## Approach

SSH info is emitted as a separate `HOST_SSH_INFO` discovery event type rather than being embedded in `DiscoveredHost`. This keeps `DiscoveredHost` lightweight (it represents data collected *without* connecting to the host) while still making SSH info available through the event stream.

### Key changes

1. **New `HOST_SSH_INFO` event type** (`libs/mng/imbue/mng/api/discovery_events.py`): A `HostSSHInfoEvent` carries `host_id` and `SSHInfo`. Emitted whenever a host is "noticed" (create, start, stop, destroy, rename, cleanup, provision) via `emit_discovery_events_for_host`, and after full discovery snapshots.

2. **`SSHInfo` moved to primitives** (`libs/mng/imbue/mng/primitives.py`): Moved from `interfaces/data_types.py` to avoid circular imports. Pure data model with no dependencies.

3. **`extract_agents_and_hosts_from_full_listing`** now returns SSH info tuples as a third element, extracted from `AgentDetails.host.ssh`.

4. **`_write_unfiltered_full_snapshot`** (`libs/mng/imbue/mng/cli/list.py`): After writing the full snapshot event, emits `HOST_SSH_INFO` events for each host with SSH info.

5. **`MngStreamManager`** (`apps/changelings/.../backend_resolver.py`): Handles both `FullDiscoverySnapshotEvent` (for agent-to-host mapping) and `HostSSHInfoEvent` (for SSH connection details) independently. Tracks `_agent_host_map` and `_ssh_by_host_id`, combining them when updating the resolver. No longer calls `mng list --json`.

## Files modified

- `libs/mng/imbue/mng/primitives.py` - SSHInfo class (moved here)
- `libs/mng/imbue/mng/interfaces/data_types.py` - SSHInfo import (re-exported)
- `libs/mng/imbue/mng/api/discovery_events.py` - HostSSHInfoEvent, emit_host_ssh_info, parser
- `libs/mng/imbue/mng/cli/list.py` - _write_unfiltered_full_snapshot emits SSH events
- `apps/changelings/.../backend_resolver.py` - MngStreamManager consumes HOST_SSH_INFO events
