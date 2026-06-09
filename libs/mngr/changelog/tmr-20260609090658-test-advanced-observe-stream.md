Fixed the e2e test fixture that wrote a duplicate `type` key into
`settings.local.toml`, which produced invalid TOML and broke
`mngr observe --discovery-only` (and other config-reloading commands) under
the e2e suite. Also strengthened `test_advanced_observe_stream` to verify the
documented DISCOVERY_FULL snapshot contract (source, agents/hosts/providers
collections, and presence of the local provider).
