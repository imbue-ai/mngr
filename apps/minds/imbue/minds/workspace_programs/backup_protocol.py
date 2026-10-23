from typing import Final

# Each backup program's answer line starts with its marker (see ``program_support.emit_marker_json``).
CHECK_RESULT_MARKER: Final[str] = "MINDS_BACKUP_CHECK_JSON:"
GATE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_GATE_JSON:"
UPDATE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_UPDATE_JSON:"
RESTORE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_RESTORE_JSON:"

# The ``status`` a mutating program (update, restore) answers with.
STATUS_OK: Final[str] = "ok"
STATUS_BLOCKED: Final[str] = "blocked"
STATUS_FAILED: Final[str] = "failed"

# The check's ``code_state``: the installed backup code against the minimum release.
CODE_STATE_MATCHES: Final[str] = "matches"
CODE_STATE_NEWER: Final[str] = "newer"
CODE_STATE_OUTDATED: Final[str] = "outdated"
CODE_STATE_UNVERIFIABLE: Final[str] = "unverifiable"

# The check's ``service_state``: the host-backup program as supervisord reports it.
SERVICE_STATE_RUNNING: Final[str] = "running"
SERVICE_STATE_NOT_RUNNING: Final[str] = "not_running"
SERVICE_STATE_UNKNOWN: Final[str] = "unknown"

# The one repository backup-service code is fetched from. Imbue Studio owns the
# ``official`` remote on every workspace: the programs create it (or repoint it)
# at this URL, deliberately ignoring ``parent.toml`` -- workspaces created from
# private template clones still receive the official backup code, and the
# ``upstream`` remote name stays reserved for the update-self machinery. Tests
# override it via the programs' ``--official-url`` argument.
OFFICIAL_REMOTE_URL: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
# The name that remote goes under, also reused by the desktop client's version
# read (``workspace_version.py``), which fetches release tags through it.
OFFICIAL_REMOTE_NAME: Final[str] = "official"
