Added `extract_agent_data_from_parsed_listing` to the shared
`providers/listing_utils` helpers, the natural companion to
`parse_listing_collection_output`. It pulls each agent's `data.json` dict out of a
parsed listing, replacing three copies of the same logic in the VPS provider's
realizers. An entry whose `data` is present but not a JSON object (a list/scalar
from a corrupt or hand-edited `data.json`) is now skipped with a WARNING rather
than silently, matching the other listing skip-sites. No user-visible behavior
change beyond the added warning.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.
