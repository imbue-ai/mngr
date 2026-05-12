# Address parsing refactor

The four shapes of address strings that mngr accepts on the command line are now
represented as separate typed values, parsed once at the CLI boundary and
threaded through the API layer as typed objects rather than raw strings.

## New address types

In `imbue.mngr.primitives` (with parsers in `imbue.mngr.api.address_parsers`):

- `HostAddress` — `HOST[.PROVIDER]` (or bare `.PROVIDER` for the new-host hint
  used by `mngr create`).
- `AgentAddress` — `NAME[@HOST[.PROVIDER]]`. Used by `mngr connect`,
  `mngr destroy`, `mngr exec`, etc. The agent component is required.
- `NewAgentLocation` — `[NAME][@HOST[.PROVIDER]][:PATH]`. The positional
  argument of `mngr create`. The name is optional (auto-generated if omitted)
  and is parsed strictly as `AgentName` (not an agent ID).
- `HostedLocation` — `[NAME[@HOST[.PROVIDER]]][:PATH]` or a bare path.
  Designates "a location on any host", local or remote. Used as the source
  argument of `mngr create --from`/`mngr pair` and as the target argument of
  `mngr push`/`mngr pull`.

Two new type aliases (in `imbue.mngr.primitives`) capture the
"name-or-id" notion at the type level:

- `AgentNameOrId = AgentId | AgentName`
- `HostNameOrId = HostId | HostName`

## CLI parses at the Click level

The Click `ParamType` adapters in `imbue.mngr.cli.address_params` (`AGENT_ADDRESS`,
`HOST_ADDRESS`, `NEW_AGENT_LOCATION`, `HOSTED_LOCATION`, `AGENT_NAME_OR_ID`,
`HOST_NAME_OR_ID`, `AGENT_NAME`) attach to `@click.argument` and
`@click.option`, so command bodies receive typed addresses directly. The
api/ layer takes the typed objects; the cli/ layer no longer holds parsing
logic that the api/ layer also needs.

## User-visible behavior changes

- **`mngr push`/`mngr pull` now accept `@HOST[.PROVIDER]:PATH` syntax** in their
  `TARGET`/`SOURCE` argument. Previously these commands had a bespoke parser
  that only understood `AGENT[:PATH]`, so a fully-qualified address like
  `mngr push my-agent@m1.modal:/path` failed with a "host filter not supported"
  error. The shared `HostedLocation` parser unifies this with the rest of the
  CLI.
- **`HostName` no longer permits dots.** Host names are validated as
  `SafeName` (alphanumeric + dashes/underscores). The `HOST.PROVIDER`
  qualifier is now exclusively the parser's responsibility -- previously
  `HostName` also carried `.provider_name` and `.short_name` properties that
  split on the dot, which contradicted the principle that "host names do not
  contain dots, so the dot is a deterministic separator."

## Internal cleanup

The following functions / types are removed; callers should use the typed
replacements:

- `parse_address_part`, `parse_host_qualifier`, `parse_source_string`,
  `parse_identifier_as_address`, `ParsedSourceLocation` -> replaced by the
  composite parsers in `api/address_parsers.py` and FrozenModel types in
  `primitives.py`.
- `find_and_maybe_start_agent_by_name_or_id` -> `find_and_maybe_start_agent`
  (takes `AgentNameOrId` instead of `str`).
- `parse_agent_spec` (cli helper for `mngr push`/`pull`) -> deleted; use
  `parse_hosted_location` / the `HOSTED_LOCATION` ParamType.
- `_host_matches_filter` -> replaced by the `HostAddress.matches` method.
- `api/agent_addr.py` was folded into `api/find.py` and `api/discover.py`; with
  typed addresses as the norm, there is no longer a reason to segregate
  address-accepting functions.
- The api-level `find_agent_by_address`, `find_agents_by_addresses`,
  `discover_by_address`, `resolve_host_reference`, `resolve_agent_reference`,
  `find_all_matching_hosts`, `find_all_matching_agents`,
  `exec_command_on_agent(s)`, etc., all take typed addresses now instead of
  raw strings.
- `AgentDetails.address` and `HostDetails.address` expose the corresponding
  typed addresses as cached properties, so callers can pass them directly to
  api functions instead of reconstructing addresses from individual fields.
