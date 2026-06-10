Fixed a duplicate `type = "claude"` key in the e2e test fixture's `settings.local.toml`, which caused every e2e test to fail at agent creation with a TOML "Cannot overwrite a value" parse error.
