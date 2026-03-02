# Logging Spec

How mng handles logging and output.

## Design Philosophy

mng separates three distinct concerns:
1. **Command Results**: Structured data output (to stdout)
2. **Console Logging**: Diagnostic information shown during execution (to stderr)
3. **File Logging**: Persistent diagnostic logs in JSONL event envelope format (to `logs/<source>/events.jsonl`)

## Command Results vs Logging

**Command Results** are the primary output of a command (e.g., agent ID, status):
- Sent to stdout
- Format controlled by `--format` flag (human, json, jsonl)
- Suppressed by `-q/--quiet`

**Console Logging** shows what's happening during execution:
- Sent to stderr
- Level controlled by `-v/--verbose` flags or config
- Shows: BUILD (default), DEBUG (-v), TRACE (-vv)
- BUILD level shows image build logs (modal, docker) in medium gray
- DEBUG level shows diagnostic messages in blue
- Suppressed by `-q/--quiet`

**File Logging** captures detailed diagnostic information:
- Saved to `logs/<source>/events.jsonl` (e.g., `~/.mng/logs/mng/events.jsonl` for the mng CLI)
- Uses the standard event envelope format (same as all other events in the system)
- Level controlled by config (default: DEBUG)
- Each log line is a self-describing JSON object with envelope fields

## Event Envelope Format

Every log line uses the same event envelope as all other structured events:

```json
{"timestamp":"2026-03-01T12:00:00.123456789Z","type":"mng","event_id":"log-...","source":"mng","level":"INFO","message":"Created agent","pid":12345,"command":"create"}
```

Envelope fields: `timestamp`, `type`, `event_id`, `source`
Log-specific fields: `level`, `message`, `pid`, `command` (optional)

The `type` field identifies the program (e.g., `mng`, `changelings`, `event_watcher`).
The `source` field matches the directory under `logs/` where events are stored.
Level names match Python's loguru: `TRACE`, `DEBUG`, `BUILD`, `INFO`, `WARNING`, `ERROR`.

Both Python (loguru) and bash scripts emit the same format using the shared `LogEvent` type (Python) and `mng_log.sh` library (bash).

## Configuration

Logging behavior is configured via the `[logging]` section in config files:

```toml
[logging]
# What gets logged to file (default: DEBUG)
file_level = "DEBUG"

# What gets shown on console during commands (default: BUILD)
# BUILD shows image build logs (modal, docker) in medium gray
console_level = "BUILD"

# Where logs are stored (relative to data root if relative)
log_dir = "logs"

# Maximum size of each log file before rotation
max_log_size_mb = 10

# Whether to log what commands were executed [future]
is_logging_commands = true

# Whether to log stdout/stderr from executed commands [future]
is_logging_command_output = false

# Whether to log environment variables (security risk) [future]
is_logging_env_vars = false
```

## CLI Options

CLI flags override config settings:

- `--format [human|json|jsonl]`: Output format for command results
- `-q, --quiet`: Suppress all console output
- `-v, --verbose`: Show DEBUG on console
- `-vv, --very-verbose`: Show TRACE on console
- `--[no-]log-commands`: Override is_logging_commands
- `--[no-]log-command-output`: Override is_logging_command_output
- `--[no-]log-env-vars`: Override is_logging_env_vars (security risk)

## Log File Management

### Location

Logs are stored at:
- `~/.mng/logs/<source>/events.jsonl` by default (e.g., `~/.mng/logs/mng/events.jsonl`)
- Configurable via `logging.log_dir` in config
- If relative, resolved relative to data root (`default_host_dir` or `~/.mng`)

### Rotation

Logs are rotated by loguru when the file exceeds `max_log_size_mb`. Rotated files are renamed with a numeric suffix by loguru's built-in rotation mechanism.

### Format

Each line is a JSON object following the EventEnvelope schema:
- `timestamp`: ISO 8601 with nanosecond precision (e.g., `2026-03-01T12:00:00.123456789Z`)
- `type`: Program/component name (e.g., `mng`, `event_watcher`, `stop_hook`)
- `event_id`: Unique identifier (e.g., `log-1709280000123456789-12345-1`)
- `source`: Matches the folder under `logs/` (e.g., `mng`, `event_watcher`)
- `level`: Log level (`TRACE`, `DEBUG`, `BUILD`, `INFO`, `WARNING`, `ERROR`)
- `message`: The log message text
- `pid`: Process ID
- `command`: CLI subcommand (optional, present for `mng` and `changelings`)

## Sensitive Data

### Environment Variable Redaction [future]

Environment variables are **redacted from logs by default** for security. This prevents accidental leakage of:
- API keys or tokens
- SSH private keys
- Passwords
- Other credentials passed via `--pass-env` or `--env`

To include environment variables in logs (e.g., for debugging), use `--log-env-vars` or set `is_logging_env_vars = true` in config. This is a security risk and should only be enabled when necessary.

### Command Output Logging

Command output logging (`is_logging_command_output`) is also disabled by default to prevent accidental leakage of sensitive data that might appear in stdout/stderr.
