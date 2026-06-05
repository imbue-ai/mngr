Renamed the plugin from `mngr_uncapped_claude` to `mngr_robinhood`. The
PyPI package is now `imbue-mngr-robinhood`, the importable package is
`imbue.mngr_robinhood`, and the CLI command is now `mngr robinhood`
(previously `mngr uncapped-claude`). Spawned agents now use the `robinhood-`
name prefix and a `created-by=robinhood` label. Every occurrence of
"uncapped" was replaced with "robinhood" (case-preserving), including error
classes (`RobinhoodError`) and CLI option types. Behavior is otherwise
unchanged.
