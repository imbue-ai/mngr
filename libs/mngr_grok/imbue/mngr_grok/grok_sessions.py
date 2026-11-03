import os
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import dedupe_by_resolved_path
from imbue.mngr.api.preservation import iter_agent_session_paths
from imbue.mngr.api.preservation import require_unique_match
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_grok.grok_config import FORK_FROM_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GROK_HOME_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.resources.grok_hook import SAFE_ID_PATTERN

_GROK_EVENT_SOURCE: Final[str] = "grok"

GROK_SESSIONS_RELATIVE_PATH: Final[Path] = GROK_HOME_RELATIVE_PATH / "sessions"
GROK_MEMORY_RELATIVE_PATH: Final[Path] = GROK_HOME_RELATIVE_PATH / "memory-v2"
ROOTS_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "roots"

# A session directory grok can resume holds this file.
SESSION_SUMMARY_FILENAME: Final[str] = "summary.json"

_HOST_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


class GrokSessionError(PluginMngrError):
    """Raised when a grok session cannot be located or placed for an agent."""


class GrokSessionSource(FrozenModel):
    """A resumable grok session directory that an agent can adopt."""

    session_id: str = Field(description="The session's id, which is also its directory name")
    session_dir: Path = Field(description="The session directory, holding its summary.json")


@pure
def is_valid_grok_session_id(candidate: str) -> bool:
    return SAFE_ID_PATTERN.match(candidate) is not None


@pure
def build_grok_preserved_items(is_memory_enabled: bool) -> list[PreservedItem]:
    """The files kept from a grok agent's state dir when it is destroyed."""
    items = [
        *build_transcript_preserved_items(_GROK_EVENT_SOURCE),
        PreservedItem(rel_path=GROK_SESSIONS_RELATIVE_PATH.as_posix(), kind=FileType.DIRECTORY),
        PreservedItem(rel_path=SESSION_ID_RELATIVE_PATH.as_posix(), kind=FileType.FILE),
        PreservedItem(rel_path=ROOTS_RELATIVE_PATH.as_posix(), kind=FileType.FILE),
    ]
    if is_memory_enabled:
        items.append(PreservedItem(rel_path=GROK_MEMORY_RELATIVE_PATH.as_posix(), kind=FileType.DIRECTORY))
    return items


@pure
def build_resumable_session_dirs_command(sessions_dir: Path, session_id: str) -> str:
    """Shell command printing every `<sessions_dir>/<bucket>/<session_id>` that holds a summary.json, one per line."""
    return (
        f"for session_dir in {shlex.quote(str(sessions_dir))}/*/{shlex.quote(session_id)}; do "
        f'if [ -f "$session_dir/{SESSION_SUMMARY_FILENAME}" ]; then printf \'%s\\n\' "$session_dir"; fi; '
        "done"
    )


@pure
def parse_resumable_session_dirs(command_stdout: str) -> list[Path]:
    return [Path(line) for line in command_stdout.splitlines() if line.strip() != ""]


def find_resumable_session_dir(host: OnlineHostInterface, sessions_dir: Path, session_id: str) -> Path | None:
    """The first bucket's directory of a resumable session under a grok home's sessions dir, or None."""
    result = host.execute_idempotent_command(
        build_resumable_session_dirs_command(sessions_dir, session_id), timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS
    )
    if not result.success:
        raise GrokSessionError(
            f"Could not search {sessions_dir} for grok session {session_id} (stderr={result.stderr!r})"
        )
    session_dirs = parse_resumable_session_dirs(result.stdout)
    return session_dirs[0] if session_dirs else None


def read_recorded_grok_session_id(host: OnlineHostInterface, agent_state_dir: Path) -> str | None:
    """The session id a grok agent resumes (its `plugin/grok/session_id`), or None when it has none."""
    try:
        recorded = host.read_text_file(agent_state_dir / SESSION_ID_RELATIVE_PATH).strip()
    except FileNotFoundError:
        return None
    return recorded or None


def resolve_canonical_work_dir(host: OnlineHostInterface, work_dir: Path) -> str:
    """The work dir with every symlink resolved on the host, the path grok names its session bucket after."""
    quoted_work_dir = shlex.quote(str(work_dir))
    result = host.execute_idempotent_command(
        f"cd {quoted_work_dir} && pwd -P", timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS
    )
    canonical_work_dir = result.stdout.strip()
    if not result.success or not canonical_work_dir.startswith("/"):
        raise GrokSessionError(
            f"Could not resolve the canonical path of work dir {work_dir} on the host (stdout={result.stdout!r}, "
            f"stderr={result.stderr!r})"
        )
    return canonical_work_dir


@pure
def build_destination_session_dir(agent_state_dir: Path, canonical_work_dir: str, session_id: str) -> Path:
    """Where an adopted or cloned session goes so grok resumes it from the agent's work dir."""
    return agent_state_dir / GROK_SESSIONS_RELATIVE_PATH / compute_session_bucket_name(canonical_work_dir) / session_id


def copy_cloned_grok_session(
    dest_host: OnlineHostInterface,
    dest_agent_state_dir: Path,
    canonical_work_dir: str,
    source_location: HostLocation,
) -> str | None:
    """Copy the session a `--from` source agent resumes into this agent's bucket; None when it has none."""
    source_session_id = read_recorded_grok_session_id(source_location.host, source_location.path)
    if source_session_id is None:
        logger.warning(
            "Clone source agent {} records no grok session to resume; the clone starts a fresh session",
            source_location.path,
        )
        return None
    if not is_valid_grok_session_id(source_session_id):
        logger.warning(
            "Clone source agent {} records {!r}, which is not a grok session id; the clone starts a fresh session",
            source_location.path,
            source_session_id,
        )
        return None
    source_session_dir = find_resumable_session_dir(
        source_location.host, source_location.path / GROK_SESSIONS_RELATIVE_PATH, source_session_id
    )
    if source_session_dir is None:
        logger.warning(
            "Clone source agent {} has no resumable copy of grok session {}; the clone starts a fresh session",
            source_location.path,
            source_session_id,
        )
        return None
    dest_session_dir = build_destination_session_dir(dest_agent_state_dir, canonical_work_dir, source_session_id)
    with log_span("Copying cloned grok session {} into {}", source_session_id, dest_session_dir):
        dest_host.copy_directory(source_location.host, source_session_dir, dest_session_dir)
    return source_session_id


def copy_adopted_grok_session(
    dest_host: OnlineHostInterface,
    local_host: OnlineHostInterface,
    dest_agent_state_dir: Path,
    canonical_work_dir: str,
    source: GrokSessionSource,
) -> str:
    """Copy a locally resolved `--adopt` session into this agent's bucket and return its id."""
    dest_session_dir = build_destination_session_dir(dest_agent_state_dir, canonical_work_dir, source.session_id)
    with log_span("Copying adopted grok session {} into {}", source.session_id, dest_session_dir):
        dest_host.copy_directory(local_host, source.session_dir, dest_session_dir)
    return source.session_id


def record_grok_fork_source(host: OnlineHostInterface, agent_state_dir: Path, session_id: str) -> None:
    """Make the agent's next launch fork `session_id` into its own session id."""
    host.write_text_file(agent_state_dir / FORK_FROM_RELATIVE_PATH, session_id + "\n")


def get_local_user_grok_home() -> Path:
    """The local user's grok home, `${GROK_HOME:-$HOME/.grok}`; an inherited GROK_HOME is ignored inside a grok tool."""
    grok_home_override = os.environ.get("GROK_HOME", "") if os.environ.get("GROK_AGENT", "") == "" else ""
    return Path(grok_home_override) if grok_home_override else Path.home() / ".grok"


def _list_local_grok_session_stores(mngr_ctx: MngrContext, user_grok_home: Path) -> list[Path]:
    """The user's own sessions dir and every live or preserved local grok agent's, without duplicates."""
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return dedupe_by_resolved_path(
        [user_grok_home / "sessions", *iter_agent_session_paths(local_host_dir, GROK_SESSIONS_RELATIVE_PATH)]
    )


@pure
def _build_adoptable_session_source(session_dir: Path) -> GrokSessionSource:
    return GrokSessionSource(session_id=session_dir.name, session_dir=session_dir)


def _find_local_resumable_session_dirs(session_stores: Sequence[Path], session_id: str) -> list[Path]:
    return [
        session_dir
        for store in session_stores
        if store.is_dir()
        for session_dir in sorted(store.glob(f"*/{session_id}"))
        if (session_dir / SESSION_SUMMARY_FILENAME).is_file()
    ]


def resolve_grok_adopt_session(adopt_arg: str, mngr_ctx: MngrContext, user_grok_home: Path) -> GrokSessionSource:
    """Resolve an `--adopt` value, a session id or an absolute session directory, on the local machine."""
    candidate = Path(adopt_arg)
    if candidate.is_absolute():
        session_dir = candidate.resolve()
        if not (session_dir / SESSION_SUMMARY_FILENAME).is_file() or not is_valid_grok_session_id(session_dir.name):
            raise UserInputError(
                f"{session_dir} is not a resumable grok session directory (it needs a {SESSION_SUMMARY_FILENAME})"
            )
        return _build_adoptable_session_source(session_dir)
    if not is_valid_grok_session_id(adopt_arg):
        raise UserInputError(f"{adopt_arg!r} is not a grok session id or an absolute session directory")
    matches = _find_local_resumable_session_dirs(_list_local_grok_session_stores(mngr_ctx, user_grok_home), adopt_arg)
    session_dir = require_unique_match(
        matches,
        not_found_message=(
            f"Grok session {adopt_arg} not found. Check the session id, or pass the absolute path of the session "
            f"directory. (Searched {user_grok_home / 'sessions'} and every live and preserved grok agent on this "
            "machine.)"
        ),
        ambiguous_message=(
            f"Grok session {adopt_arg} is in more than one place; pass the absolute path of the session directory "
            "to adopt:"
        ),
    )
    return _build_adoptable_session_source(session_dir)
