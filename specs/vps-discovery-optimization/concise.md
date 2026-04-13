# VPS Docker Provider Discovery Optimization

## Overview

- `mngr list` with the Vultr provider takes ~21 seconds for a single host because host/agent discovery and detail collection each make many sequential SSH round-trips (15+ individual commands per host)
- The modal provider solved this same problem with three optimizations: batched state reads during discovery, a single shell script for detail collection, and caching API/host-record results within a command invocation
- This spec applies the same approach to the VPS Docker base provider (`VpsDockerProvider`), benefiting Vultr and any future VPS-based providers
- The listing collection script and parser (currently modal-only) are extracted to a shared location in core mngr so both modal and VPS docker can use them
- Target: `mngr list` for a single VPS host should complete in ~2-4 seconds (down from ~21 seconds)

## Expected Behavior

- `mngr list` with the Vultr provider returns results in ~2-4 seconds for a typical setup (1-3 VPSes, 1-5 agents each)
- The Vultr API is called at most once per `mngr list` invocation (cached for the duration of the command)
- Host records and agent data from the VPS state container are read in a single batched SSH command per VPS, not N+1 individual commands
- SSH calls to multiple VPSes run in parallel using ConcurrencyGroup
- Host and agent detail collection (boot time, uptime, activity timestamps, tmux info, ps output) happens in a single `exec_in_container` call per host, not 10-15 individual SSH commands
- `DiscoveredHost` entries include `host_state` (RUNNING/STOPPED/etc.) derived during discovery, avoiding a separate SSH call later
- The modal provider continues to work identically but imports the listing script/parser from the new shared location
- No changes to the CLI interface, output format, or user-facing behavior beyond speed

## Changes

- **New file: `libs/mngr/imbue/mngr/providers/listing_utils.py`**
  - Extract from `libs/mngr_modal/imbue/mngr_modal/instance.py`: `_build_listing_collection_script`, `_parse_listing_collection_output`, `_extract_delimited_block`, `_parse_agent_section`, `_parse_optional_int`, `_parse_optional_float`, and the `_SEP_*` constants
  - These are pure functions with no modal-specific imports

- **Update: `libs/mngr_modal/imbue/mngr_modal/instance.py`**
  - Replace the extracted functions/constants with imports from `listing_utils.py`
  - No behavioral change

- **Update: `libs/mngr_vps_docker/imbue/mngr_vps_docker/host_store.py`**
  - Replace `list_all_host_records` with a batched version that reads all host JSON files in a single SSH command (e.g. `for f in *.json; do echo "---FILE:$f---"; cat "$f"; done`)
  - Similarly batch `list_persisted_agent_data_for_host` into a single SSH command that reads all agent JSON files at once
  - These methods should also return agent data alongside host records when requested, to avoid a separate round-trip

- **Update: `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`**
  - Add host record and Vultr API response caching via `PrivateAttr` dicts on `VpsDockerProvider`, matching modal's pattern (`_host_record_cache_by_id`, etc.)
  - Update `reset_caches()` to clear these new caches
  - Override `discover_hosts_and_agents`:
    - Call `_discover_host_records()` (which queries the API then SSHes to each VPS in parallel via ConcurrencyGroup)
    - Read persisted agent data from the state container in the same batched SSH call
    - Build `DiscoveredHost` with `host_state` set (using container-running check already performed during discovery)
    - Build `DiscoveredAgent` refs from persisted agent data using `validate_and_create_discovered_agent`
    - Cache all host records for reuse by `get_host_and_agent_details`
  - Override `get_host_and_agent_details`:
    - Look up the cached host record (no re-reading from VPS)
    - Run the shared `_build_listing_collection_script` via `docker_ssh.exec_in_container` (single SSH command)
    - Parse output with shared `_parse_listing_collection_output`
    - Build `HostDetails` and `AgentDetails` from parsed data (provider-specific `_build_host_details_from_raw` and `_build_agent_details_from_raw` methods)
    - Fall back to `super().get_host_and_agent_details` for offline hosts
  - Parallelize `_discover_host_records` SSH calls across VPSes using ConcurrencyGroupExecutor

- **Update: `libs/mngr_vultr/imbue/mngr_vultr/backend.py`**
  - `_discover_host_records` already overrides the base; update it to use the parallelized pattern and populate the host record cache
  - Add Vultr API response caching (`list_instances` result cached for duration of command)
