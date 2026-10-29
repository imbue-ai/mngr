from enum import StrEnum
from enum import auto
from typing import Final

# Each backup program's answer line starts with its marker (see ``program_support.emit_marker_json``).
CHECK_RESULT_MARKER: Final[str] = "MINDS_BACKUP_CHECK_JSON:"
GATE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_GATE_JSON:"
UPDATE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_UPDATE_JSON:"
RESTORE_RESULT_MARKER: Final[str] = "MINDS_BACKUP_RESTORE_JSON:"
VERIFICATION_FLAG_RESULT_MARKER: Final[str] = "MINDS_BACKUP_VERIFICATION_FLAG_JSON:"


class BackupProgramStatus(StrEnum):
    """The ``status`` a mutating backup program (update, restore) answers with."""

    OK = auto()
    BLOCKED = auto()
    FAILED = auto()


class BackupCodeState(StrEnum):
    """The check's ``code_state``: the installed backup code against the minimum release."""

    MATCHES = auto()
    NEWER = auto()
    OUTDATED = auto()
    UNVERIFIABLE = auto()


class BackupServiceState(StrEnum):
    """The check's ``service_state``: the host-backup program as supervisord reports it."""

    RUNNING = auto()
    NOT_RUNNING = auto()
    UNKNOWN = auto()


def parse_backup_program_status(raw: object) -> BackupProgramStatus:
    """The status an answer carries; a missing or unknown one reads as FAILED."""
    try:
        return BackupProgramStatus(raw)
    except ValueError:
        return BackupProgramStatus.FAILED


def parse_backup_code_state(raw: object) -> BackupCodeState:
    """The code state a check answer carries; a missing or unknown one reads as UNVERIFIABLE."""
    try:
        return BackupCodeState(raw)
    except ValueError:
        return BackupCodeState.UNVERIFIABLE


def parse_backup_service_state(raw: object) -> BackupServiceState:
    """The service state a check answer carries; a missing or unknown one reads as UNKNOWN."""
    try:
        return BackupServiceState(raw)
    except ValueError:
        return BackupServiceState.UNKNOWN


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
