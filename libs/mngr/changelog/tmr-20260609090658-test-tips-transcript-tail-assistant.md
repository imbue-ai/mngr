Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a duplicate
`type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The
duplicate key made the file invalid TOML, so every `mngr` command run through the e2e fixture
failed with "Cannot overwrite a value". Removing the duplicate restores the e2e tutorial tests
(including `test_tips_transcript_tail_assistant`).
