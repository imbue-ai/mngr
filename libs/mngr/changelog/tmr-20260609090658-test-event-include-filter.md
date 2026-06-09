Fixed the e2e test fixture that generated an invalid `settings.local.toml`: the
`[commands.create]` table contained a duplicate `type = "claude"` key, which made
tomlkit reject the file ("Cannot overwrite a value") and caused every `mngr create`
in the e2e tutorial tests to fail. Removed the duplicate key. Also removed a
shadowing duplicate `_parse_jsonl_events` helper in the event tutorial tests so the
stricter (object-asserting) parser is the one actually used.
