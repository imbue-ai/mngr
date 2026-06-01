imbue_cloud workspace creation now falls back automatically. Minds runs
`mngr create` with `fast_mode=require` first (adopt a matching pre-baked pool
host); if no exact match is available the provider raises
`FastPathUnavailableError`, and minds retries the same create with
`fast_mode=prevent`, which leases any available pool host and rebuilds it from
the FCT Dockerfile. The user-facing creation log states which path was taken.
