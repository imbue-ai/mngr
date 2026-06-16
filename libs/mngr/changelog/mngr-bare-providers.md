Added `extract_agent_data_from_parsed_listing` to the shared
`providers/listing_utils` helpers, the natural companion to
`parse_listing_collection_output`. It pulls each agent's `data.json` dict out of a
parsed listing (skipping malformed entries), replacing three copies of the same
logic in the VPS provider's realizers. No user-visible behavior change.
