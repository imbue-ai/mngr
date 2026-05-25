# Drop `minds_api_key` from the imbue_cloud inject helpers

`build_combined_inject_command` and `normalize_inject_args` no longer
take a `minds_api_key` argument. The minds desktop client now uses a
single central `MINDS_API_KEY` per installation; the latchkey
gateway's `minds-api-proxy` extension injects it as
`Authorization: Bearer <key>` on every forwarded request, and agents
themselves never see the value -- so there is nothing to push down
onto a leased pool host.
