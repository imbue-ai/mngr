# Offline agent field generators

Updated the provider's `get_host_and_agent_details` override (and its lease-only `_build_offline_details_from_lease` fallback) to accept and forward the new `offline_field_generators` parameter, so offline plugin fields (see the mngr changelog entry) are populated for leased hosts that fall back to offline/lease-only data.
