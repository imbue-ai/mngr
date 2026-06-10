Tab completion now completes the `-S`/`--setting` config override (`KEY=VALUE`) on every command.

- Pressing TAB after `-S` (or `--setting`) completes the config KEY against the same catalog of keys behind `mngr config set` (e.g. `mngr create -S head<TAB>` -> `headless=`). Keys with a constrained value set (booleans, enums like log levels, provider/agent-type names) insert `KEY=` and then list the allowed values on the next TAB; free-form keys complete to the bare key name.

- Values complete too: `mngr create -S logging.console_level=<TAB>` lists `TRACE`/`DEBUG`/... and `mngr create -S headless=<TAB>` lists `true`/`false`. Works in both zsh and bash (which tokenize `KEY=VALUE` differently).

- Fixed a related completion bug: short value-taking options (`-S`, `-m`, `-b`, `-l`, `-n`, `-t`, `-o`, `-i`, `-s`, `-w`) were not recognized as consuming their value, so their argument was miscounted as a positional. This could suppress completion of a later positional argument on commands with a fixed argument count (e.g. after `-S KEY=VALUE`). The short forms of value-taking options are now recorded for completion.
