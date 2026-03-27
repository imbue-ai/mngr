# Logging Spec

How mngr handles logging and output.

## Design Philosophy

mngr separates three distinct concerns:
1. **Command Results**: Structured data output (to stdout)
2. **Console Logging**: Diagnostic information shown during execution (to stderr)
3. **File Logging**: Persistent diagnostic logs in JSONL event envelope format (to `events/<source>/events.jsonl`)

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
- Saved to `events/<source>/events.jsonl` (e.g., `~/.mngr/events/logs/mngr/events.jsonl` for the mngr CLI)
- Uses the standard event envelope format (same as all other events in the system)
- Level controlled by config (default: DEBUG)
- Each log line is a self-describing JSON object with envelope fields

## Log Format

Both Python and bash emit flat JSON with the same top-level field names. The envelope fields (`timestamp`, `type`, `event_id`, `source`, `level`, `message`, `pid`) are at the top level in both, so a single parser works for all log lines. Python logs include additional fields (function, line, module, etc.) that bash logs don't have.

### Bash example (envelope fields only)

```json
{"timestamp":"2026-03-01T12:00:00.123456789Z","type":"event_watcher","event_id":"evt-a1b2c3d4...","source":"event_watcher","level":"INFO","message":"Started watching","pid":12345}
```

### Python example (envelope fields + loguru metadata)

```json
{"timestamp":"2026-03-01T12:00:00.123456789Z","type":"mngr","event_id":"evt-a1b2c3d4...","source":"mngr","level":"INFO","message":"Created agent","pid":12345,"command":"create","function":"create_agent","line":42,"module":"api.create","logger_name":"imbue.mngr.api.create","file_name":"create.py","file_path":"/path/to/create.py","elapsed_seconds":0.012,"exception":null,"process_name":"MainProcess","thread_name":"MainThread","thread_id":140106487883584,"extra":{"host":"my-host"}}
```

### All fields

Shared (present in both Python and bash):
- `timestamp`: ISO 8601 with nanosecond precision in UTC
- `type`: Program/component name (e.g., `mngr`, `event_watcher`, `stop_hook`)
- `event_id`: Unique identifier (e.g., `evt-a1b2c3d4e5f67890a1b2c3d4e5f67890`)
- `source`: Matches the folder under `events/`
- `level`: Log level string (`TRACE`, `DEBUG`, `BUILD`, `INFO`, `WARNING`, `ERROR`)
- `message`: The log message text
- `pid`: Process ID

Optional shared:
- `command`: CLI subcommand (present for `mngr` and `minds`)

Python-only (from loguru):
- `function`, `line`, `module`, `logger_name`: Source code location
- `file_name`, `file_path`: Source file
- `elapsed_seconds`: Time since logger was first used
- `exception`: Exception info object (null when no exception)
- `process_name`, `thread_name`, `thread_id`: Process and thread info
- `extra`: Dict of context from `logger.contextualize()` or `logger.bind()`

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
log_dir = "events"

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
- `~/.mngr/events/<source>/events.jsonl` by default (e.g., `~/.mngr/events/logs/mngr/events.jsonl`)
- Configurable via `logging.log_dir` in config
- If relative, resolved relative to data root (`default_host_dir` or `~/.mngr`)

### Rotation

Logs are rotated when the file exceeds `max_log_size_mb`. The rotation is handled by the custom JSONL file sink (not loguru's built-in rotation, since we use a callable sink to bypass loguru's colorizer). Rotated files are renamed with a numeric suffix (e.g., `events.jsonl.1`, `events.jsonl.2`).

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
