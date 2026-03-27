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
- `--[no-]log-command-output`: Log stdout/stderr from executed commands [default: from config]
- `--[no-]log-env-vars`: Log environment variables (security risk, disabled by default)

Environment variables are redacted from logs by default for security. Use `--log-env-vars` to include them.

### Other Options

- `--headless`: Disable all interactive behavior (prompts, TUI, editor). Also settable via `MNGR_HEADLESS` env var or `headless` config key. When not set, interactive mode is auto-detected from the TTY.
- `--context PATH`: Project context directory (used for build context and loading project-specific config) [default: local .git root]
- `--plugin TEXT / --enable-plugin TEXT / --disable-plugin TEXT`: Enable / disable selected plugins
