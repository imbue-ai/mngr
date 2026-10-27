"""The chats a workspace's background-task marker files hold live tasks for, read without the template's CLI.

A template from before minds-v0.8.7 has no ``system/scripts/background_tasks.py``, but an update run in it
still leaves markers: the staged update-self skill's ``run_in_background.py`` writes one per pending command
under ``data/.apps/chat/background_tasks/<chat-id>/``. This reads them as the template's
``chat_background_tasks.markers`` does: a marker counts while the pid it names is alive and, where ``/proc``
reports one, still has the start time the marker recorded.
"""

# CLEANUP: delete this module, the update program's ``busy`` mode and the fallbacks that call them, once
# ``OLDEST_IN_PLACE_UPDATABLE_VERSION`` reaches minds-v0.8.7, the first release that ships
# ``system/scripts/background_tasks.py``.

import json
import os
from typing import Final

from imbue.minds.workspace_programs.program_support import as_json_object

# Relative to the workspace root, where ``mngr exec`` lands and cron's tick runs.
MARKER_ROOT: Final[str] = "data/.apps/chat/background_tasks"
_MARKER_SUFFIX: Final[str] = ".json"
# ``/proc/<pid>/stat`` field 22, the process's start time, counted among the fields after the command name.
_START_TIME_FIELD_INDEX: Final[int] = 19


def _process_start_time(pid: int) -> str:
    """When the process started, in clock ticks since boot, or '' where ``/proc`` does not have it."""
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as handle:
            stat = handle.read()
    except OSError:
        return ""
    # The command name is parenthesized and may hold spaces; the numeric fields follow it.
    fields = stat.rpartition(")")[2].split()
    return fields[_START_TIME_FIELD_INDEX] if len(fields) > _START_TIME_FIELD_INDEX else ""


def _is_process_live(pid: int, pid_start: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, and another user's.
        pass
    except OverflowError:
        return False
    return not pid_start or _process_start_time(pid) in ("", pid_start)


def _is_marker_live(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            marker = as_json_object(json.load(handle))
    except (OSError, ValueError):
        return False
    if marker is None:
        return False
    pid = marker.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    pid_start = marker.get("pid_start", "")
    return _is_process_live(pid, pid_start if isinstance(pid_start, str) else "")


def busy_chat_ids(marker_root: str = MARKER_ROOT) -> frozenset[str]:
    """Every chat whose directory under ``marker_root`` holds a live marker; none where there is no such root."""
    try:
        with os.scandir(marker_root) as entries:
            chat_dirs = [entry for entry in entries if entry.is_dir()]
    except OSError:
        return frozenset()
    busy: set[str] = set()
    for chat_dir in chat_dirs:
        try:
            names = os.listdir(chat_dir.path)
        except OSError:
            continue
        markers = [name for name in names if name.endswith(_MARKER_SUFFIX) and not name.startswith(".")]
        if any(_is_marker_live(os.path.join(chat_dir.path, name)) for name in markers):
            busy.add(chat_dir.name)
    return frozenset(busy)
