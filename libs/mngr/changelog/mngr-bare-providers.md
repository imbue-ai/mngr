Added `extract_agent_data_from_parsed_listing` to the shared
`providers/listing_utils` helpers, the natural companion to
`parse_listing_collection_output`. It pulls each agent's `data.json` dict out of a
parsed listing (skipping malformed entries), replacing three copies of the same
logic in the VPS provider's realizers. No user-visible behavior change.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.
