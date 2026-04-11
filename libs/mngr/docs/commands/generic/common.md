## Common Options

### Output Format

- `--format [human|json|jsonl|FORMAT]`: Output format for command results. Many commands also accept a format template string. When a template is provided, fields use `{field.name}` syntax and shell escape sequences (`\t`, `\n`) are interpreted. One line is output per item. Example: `mngr list --format '{name}\t{state}'`. See each command's help for available fields. [default: human]

Command results are sent to stdout. Console logging is sent to stderr.

### Console Logging

- `-q, --quiet`: Suppress all console output
- `-v, --verbose`: Show DEBUG level logs on console (can be repeated: `-vv` for TRACE level)

### File Logging

Logs are automatically saved to `~/.mngr/events/logs/<timestamp>-<pid>.json` with rotation based on config settings.

- `--log-file PATH`: Override the log file path (e.g., `/tmp/mngr.log`)
- `--[no-]log-commands`: Log what commands were executed [default: from config]

### Other Options

- `--headless`: Disable all interactive behavior (prompts, TUI, editor). Also settable via `MNGR_HEADLESS` env var or `headless` config key. When not set, interactive mode is auto-detected from the TTY.
- `--plugin TEXT / --enable-plugin TEXT / --disable-plugin TEXT`: Enable / disable selected plugins
- `-S KEY=VALUE / --setting KEY=VALUE`: Override a config setting for this invocation. Uses dot-separated paths for nested keys (e.g., `--setting commands.create.connect=false`). Values are parsed as JSON when possible (booleans, numbers, arrays), otherwise as plain strings. Repeatable.
