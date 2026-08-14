Custom services (the ones minds ships itself, currently `claude.ai`) now describe their latchkey registration in latchkey's own format, and minds copies it into latchkey's config without interpreting it.

Previously the bundled definition used its own field names and Python models translated them into latchkey's shape. Two model classes and the translation step are gone: latchkey owns that schema and validates it when it loads the config, so registering a new custom service — or picking up a registration field a later latchkey release adds — is now a data-only change.

No behavior change: the config written for `claude.ai` is byte-for-byte what it was.
