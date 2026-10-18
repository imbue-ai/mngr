"""The ``app.json`` record a running desktop client leaves beside its socket.

A same-machine agent reads it to learn where the API socket is (the length
fallback can move it off the canonical path), which version is running, and how
to launch this install in the background when it finds nothing running. It
holds no secret and is advisory: a crash leaves a stale copy behind, so a
reader treats the socket as the truth and this file as the hint. The design is
``specs/imbue-studio-agent-access/spec.md``, Part 2.
"""

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MindError
from imbue.mngr.utils.file_utils import atomic_write

APP_RECORD_FILENAME: Final[str] = "app.json"

# The Electron shell hands the backend the command that launches this install in
# background mode, as a JSON array of strings; a bare ``minds run`` has none.
APP_LAUNCH_ARGV_ENV_VAR: Final[str] = "MINDS_APP_LAUNCH_ARGV"


class AppLaunchCommandError(MindError, ValueError):
    """Raised when the launcher handed down a launch command that is not a JSON array of strings."""


class AppLaunchCommand(FrozenModel):
    """A command that starts this install in background mode and returns, leaving it detached."""

    argv: tuple[str, ...] = Field(description="The command and its arguments, run as-is without a shell")


class AppRecord(FrozenModel):
    """What a running desktop client publishes about itself for same-machine callers."""

    tier: str = Field(description="The environment tier, e.g. production or staging")
    root_name: str = Field(description="The MINDS_ROOT_NAME this process runs as")
    version: str = Field(description="The desktop app version")
    pid: int = Field(description="The backend process id")
    started_at: datetime = Field(description="When this backend started, UTC")
    socket_path: Path = Field(description="Where the same-machine API socket is bound")
    http_url: str = Field(description="The loopback HTTP origin the browser UI uses")
    launch: AppLaunchCommand | None = Field(
        default=None, description="How to start this install in background mode; absent for a bare minds run"
    )


def parse_launch_command_from_environ(environ: Mapping[str, str]) -> AppLaunchCommand | None:
    """The launch command the shell handed down, or None when none was.

    Raises ``AppLaunchCommandError`` on a malformed value: the shell is ours, so
    a bad value is a launcher bug to surface rather than something to guess at.
    """
    raw = environ.get(APP_LAUNCH_ARGV_ENV_VAR)
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AppLaunchCommandError(f"{APP_LAUNCH_ARGV_ENV_VAR} is not valid JSON: {e}") from e
    if not isinstance(parsed, list) or not parsed or not all(isinstance(part, str) for part in parsed):
        raise AppLaunchCommandError(f"{APP_LAUNCH_ARGV_ENV_VAR} must be a non-empty JSON array of strings")
    return AppLaunchCommand(argv=tuple(parsed))


def app_record_path(state_dir: Path) -> Path:
    return state_dir / APP_RECORD_FILENAME


def write_app_record(path: Path, record: AppRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, record.model_dump_json(indent=2) + "\n")


def remove_app_record(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not remove the app record at {}: {}", path, e)


def read_app_record(path: Path) -> AppRecord:
    return AppRecord.model_validate_json(path.read_text())


def build_app_record(
    tier: str,
    root_name: str,
    version: str,
    socket_path: Path,
    http_url: str,
    launch: AppLaunchCommand | None,
    started_at: datetime,
) -> AppRecord:
    return AppRecord(
        tier=tier,
        root_name=root_name,
        version=version,
        pid=os.getpid(),
        started_at=started_at,
        socket_path=socket_path,
        http_url=http_url,
        launch=launch,
    )
