# Refactoring the Listing / Discovery System

## Problem Statement

The code that handles listing hosts and agents is correct and fast, but its organization is confusing. The functionality is spread across `api/list.py`, `interfaces/provider_instance.py`, and provider implementations, with unclear naming that obscures what each piece does.

There are two fundamentally distinct operations happening:

1. **Discovery**: Get lightweight references for all hosts and agents across all providers. This is needed by ~17 callers (find, connect, destroy, exec, message, rename, etc.) that just need to resolve a name/ID to a reference.

2. **Enrichment**: Get full detailed info for specific hosts and agents. This is only needed by `mng list` (and kanpan/schedule plugins) for display purposes.

Currently these two operations are interleaved in `api/list.py`, and the naming does not make the distinction clear.

## Full Refactoring Plan

The plan is organized into 5 commits. Each is a safe, mechanical refactor with no behavioral changes.

### Commit 1: Rename data types

Rename the four core data types to clearly distinguish discovery results from full details.

| Current | New | File |
|---|---|---|
| `HostReference` | `DiscoveredHost` | `primitives.py` |
| `AgentReference` | `DiscoveredAgent` | `primitives.py` |
| `HostInfo` | `HostDetails` | `interfaces/data_types.py` |
| `AgentInfo` | `AgentDetails` | `interfaces/data_types.py` |

Files that import/use `HostReference` or `AgentReference` (22 files):
- `primitives.py`, `primitives_test.py`
- `interfaces/host.py`, `interfaces/provider_instance.py`
- `hosts/host.py`, `hosts/offline_host.py`
- `providers/modal/instance.py`, `providers/modal/instance_test.py`
- `api/list.py`, `api/list_test.py`, `api/test_list.py`, `api/find.py`, `api/find_test.py`, `api/test_find.py`, `api/message.py`
- `cli/agent_utils.py`, `cli/agent_utils_test.py`, `cli/create.py`, `cli/create_test.py`, `cli/destroy.py`, `cli/limit.py`, `cli/pull_test.py`

Files that import/use `HostInfo` or `AgentInfo` (21 files):
- `interfaces/data_types.py`, `interfaces/data_types_test.py`, `interfaces/provider_instance.py`
- `providers/modal/instance.py`
- `api/list.py`, `api/list_test.py`, `api/test_list.py`, `api/cleanup.py`, `api/cleanup_test.py`, `api/data_types.py`, `api/gc.py`
- `cli/list.py`, `cli/list_test.py`, `cli/cleanup.py`, `cli/cleanup_test.py`, `cli/conftest.py`, `cli/connect.py`, `cli/test_connect.py`, `cli/gc_test.py`

External consumers (also need updating):
- `libs/mng_kanpan/imbue/mng_kanpan/fetcher.py`, `fetcher_test.py`
- `libs/mng_tutor/imbue/mng_tutor/checks.py`
- `apps/sculptor_web/imbue/sculptor_web/data_types.py`, `main.py`

This is a pure find-and-replace across all files. No logic changes.

### Commit 2: Rename methods on provider and host interfaces

**On `ProviderInstanceInterface`** (`interfaces/provider_instance.py`):

| Current | New | Rationale |
|---|---|---|
| `load_agent_refs(cg, include_destroyed)` | `discover_hosts_and_agents(cg, include_destroyed)` | Discovers both hosts and agents, not just agent refs |
| `build_host_listing_data(host_ref, agent_refs)` | `get_host_and_agent_details(host_ref, agent_refs)` | Describes what it returns (details), not what it's "for" (listing) |

Files affected by `load_agent_refs` rename (5 files):
- `interfaces/provider_instance.py` (definition + default impl)
- `providers/modal/instance.py` (override)
- `providers/modal/instance_test.py` (tests)
- `hosts/offline_host_test.py` (tests)
- `api/list.py` (callers)

Files affected by `build_host_listing_data` rename (3 files):
- `interfaces/provider_instance.py` (definition + default impl)
- `providers/modal/instance.py` (override)
- `api/list.py` (caller)

**On `HostInterface`** (`interfaces/host.py`):

| Current | New | Rationale |
|---|---|---|
| `get_agent_references()` | `discover_agents()` | Consistent with the discovery vocabulary. This discovers what agents exist on this host. |

Files affected (11 files):
- `interfaces/host.py` (abstract definition)
- `interfaces/provider_instance.py` (called in default `discover_hosts_and_agents`)
- `hosts/host.py` (online implementation)
- `hosts/host_test.py` (tests)
- `hosts/offline_host.py` (offline implementation)
- `hosts/offline_host_test.py` (tests)
- `cli/destroy.py` (caller)
- `api/find.py` (caller)
- `api/gc.py` (caller)
- `api/list.py` (caller)

**Helper function** (`hosts/offline_host.py`):

| Current | New | Rationale |
|---|---|---|
| `validate_and_create_agent_reference()` | `validate_and_create_discovered_agent()` | Matches the new type name |

Files affected (4 files):
- `hosts/offline_host.py` (definition)
- `hosts/offline_host_test.py` (tests)
- `hosts/host.py` (caller)
- `providers/modal/instance.py` (caller)

**Modal provider internals** (`providers/modal/instance.py`):

| Current | New | Rationale |
|---|---|---|
| `_build_host_info_from_raw()` | `_build_host_details_from_raw()` | Matches `HostDetails` type name |
| `_build_agent_infos_from_raw()` | `_build_agent_details_from_raw()` | Matches `AgentDetails` type name |

These are private to the Modal provider, so only 1 file affected.

### Commit 3: Move discovery to `api/discover.py`

Create a new module `api/discover.py` and move the general-purpose discovery function out of `api/list.py`.

**Move from `api/list.py` to `api/discover.py`:**
- `load_all_agents_grouped_by_host()` -- renamed to `discover_all_hosts_and_agents()`
- `_process_provider_for_host_listing()` -- renamed to `_discover_provider_hosts_and_agents()` (private helper, moves with its caller)
- `_warn_on_duplicate_host_names()` -- used by both discovery and streaming listing, so it stays shared. Could live in `discover.py` and be imported by `list.py`, or be extracted to a small utility.

**Update all 17 callers** that currently import `load_all_agents_grouped_by_host` from `api/list.py`:

Within `libs/mng`:
- `api/list.py` (still calls it internally)
- `api/list_test.py`
- `api/events.py`, `api/events_test.py`
- `api/exec.py`
- `api/find.py`
- `api/message.py`
- `cli/agent_utils.py`, `cli/test_agent_utils.py`
- `cli/connect.py`
- `cli/create.py`
- `cli/destroy.py`
- `cli/limit.py`
- `cli/rename.py`
- `cli/snapshot.py`

External:
- `libs/mng_kanpan/imbue/mng_kanpan/fetcher.py`
- `libs/mng_schedule/imbue/mng_schedule/implementations/modal/verification.py`

### Commit 4: Rename internal functions in `api/list.py`

These are all private functions, so only `api/list.py` (and its tests) are affected.

| Current | New | Rationale |
|---|---|---|
| `_assemble_host_info()` | `_collect_and_emit_details_for_host()` | Describes what it does (collects details, emits via callbacks) and scope (per host). Covers both host and agent details. |
| `_process_host_for_agent_listing()` | `_process_host_with_error_handling()` | Makes clear this is just an error-handling wrapper around the above |
| `_process_provider_streaming()` | `_discover_and_emit_details_for_provider()` | Clarifies it does both discovery and enrichment for one provider |
| `_list_agents_batch()` | `_list_agents_batch()` | Fine as-is |
| `_list_agents_streaming()` | `_list_agents_streaming()` | Fine as-is |
| `_agent_to_cel_context()` | `_agent_details_to_cel_context()` | Matches the `AgentDetails` type |
| `_apply_cel_filters(agent: AgentDetails, ...)` | `_apply_cel_filters(agent_details: AgentDetails, ...)` | Parameter rename only, matches type |

Also update the helper that builds agent details from the broken-out functions:

| Current (proposed new names) | Purpose |
|---|---|
| `_build_host_details_from_host(host, host_ref)` | Extract from `_collect_and_emit_details_for_host`, returns `HostDetails` |
| `_build_agent_details_from_online_agent(agent, host_details, activity_config, ssh_activity)` | Extract, returns `AgentDetails` |
| `_build_agent_details_from_offline_ref(agent_ref, host_details)` | Extract, returns `AgentDetails` |

### Commit 5 (optional): Create `DiscoveryResult` type

Replace the awkward `tuple[dict[DiscoveredHost, list[DiscoveredAgent]], list[BaseProviderInstance]]` return type:

```python
class DiscoveryResult(FrozenModel):
    """Result of discovering all hosts and agents across providers."""

    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = Field(
        description="Discovered agents grouped by their host"
    )
    providers: list[BaseProviderInstance] = Field(
        description="Provider instances that were queried"
    )
```

This type would live in `api/discover.py`. Most callers only need `agents_by_host` -- the current pattern of `agents_by_host, _ = discover_all_hosts_and_agents(...)` would become `discovery = discover_all_hosts_and_agents(...); agents_by_host = discovery.agents_by_host` or just `discovery.agents_by_host` inline.

## Complete rename summary

### Data types
| Current | New |
|---|---|
| `HostReference` | `DiscoveredHost` |
| `AgentReference` | `DiscoveredAgent` |
| `HostInfo` | `HostDetails` |
| `AgentInfo` | `AgentDetails` |

### Public methods
| Current | New | Where |
|---|---|---|
| `ProviderInstanceInterface.load_agent_refs()` | `.discover_hosts_and_agents()` | `interfaces/provider_instance.py` |
| `ProviderInstanceInterface.build_host_listing_data()` | `.get_host_and_agent_details()` | `interfaces/provider_instance.py` |
| `HostInterface.get_agent_references()` | `.discover_agents()` | `interfaces/host.py` |
| `load_all_agents_grouped_by_host()` | `discover_all_hosts_and_agents()` | `api/list.py` -> `api/discover.py` |
| `validate_and_create_agent_reference()` | `validate_and_create_discovered_agent()` | `hosts/offline_host.py` |

### Private functions in `api/list.py`
| Current | New |
|---|---|
| `_assemble_host_info()` | `_collect_and_emit_details_for_host()` |
| `_process_host_for_agent_listing()` | `_process_host_with_error_handling()` |
| `_process_provider_for_host_listing()` | `_discover_provider_hosts_and_agents()` (moves to `api/discover.py`) |
| `_process_provider_streaming()` | `_discover_and_emit_details_for_provider()` |
| `_agent_to_cel_context()` | `_agent_details_to_cel_context()` |

### Private functions in Modal provider
| Current | New |
|---|---|
| `_build_host_info_from_raw()` | `_build_host_details_from_raw()` |
| `_build_agent_infos_from_raw()` | `_build_agent_details_from_raw()` |

### New extractions from `_collect_and_emit_details_for_host` (commit 4)
| Function | Returns |
|---|---|
| `_build_host_details_from_host()` | `HostDetails` |
| `_build_agent_details_from_online_agent()` | `AgentDetails` |
| `_build_agent_details_from_offline_ref()` | `AgentDetails` |

### Unchanged
| Name | Why |
|---|---|
| `list_hosts()` | Already clear and accurate |
| `list_agents()` | Already clear (this is the `mng list` entry point) |
| `ListResult` | Fine -- it's the result of the list command |
| `_list_agents_batch()` | Fine |
| `_list_agents_streaming()` | Fine |
| `ErrorInfo` / `ProviderErrorInfo` / `HostErrorInfo` / `AgentErrorInfo` | These describe errors, not host/agent data |
| `SSHInfo` | SSH-specific, not part of the discovery/details split |
