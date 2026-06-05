Renamed the plugin from `mngr_uncapped_claude` to `mngr_robinhood_claude`. The
PyPI package is now `imbue-mngr-robinhood-claude`, the importable package is
`imbue.mngr_robinhood_claude`, and the CLI command is now `mngr robinhood-claude`
(previously `mngr uncapped-claude`). Spawned agents now use the `robinhood-`
name prefix and a `created-by=robinhood-claude` label. Every occurrence of
"uncapped" was replaced with "robinhood" (case-preserving), including error
classes (`RobinhoodClaudeError`) and CLI option types. Behavior is otherwise
unchanged.
