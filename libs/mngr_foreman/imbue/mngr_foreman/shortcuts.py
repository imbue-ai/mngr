"""Foreman-local front-page shortcuts: named command buttons.

Set from the CLI (``mngr foreman set-shortcut <name> "<cmd>"``) and stored on the
foreman box. The home page renders one button per shortcut; clicking it opens a
terminal tab running ``<cmd>`` with whatever the user typed into the shortcut's
freeform args box appended.

The CLI (a separate process) writes the file and the long-running server reads it,
so every operation reads/writes the file fresh -- no in-memory cache to go stale
after a ``set-shortcut`` from the command line.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict


class ShortcutStore:
    """File-backed map of shortcut name -> command string (read fresh per op)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()  # serialize this process's read-modify-writes

    def _read(self) -> dict[str, str]:
        # read_json_dict: missing/malformed/non-object -> {} (a bad file can't wedge foreman).
        return {str(k): str(v) for k, v in read_json_dict(self._path).items()}

    def _write(self, shortcuts: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(shortcuts, indent=2, sort_keys=True))

    def all(self) -> list[dict[str, str]]:
        """All shortcuts as ``[{"name", "cmd"}, ...]`` sorted by name."""
        with self._lock:
            return [{"name": n, "cmd": c} for n, c in sorted(self._read().items())]

    def set(self, name: str, cmd: str) -> None:
        with self._lock:
            shortcuts = self._read()
            shortcuts[name] = cmd
            self._write(shortcuts)

    def remove(self, name: str) -> bool:
        """Delete a shortcut; return True if it existed."""
        with self._lock:
            shortcuts = self._read()
            existed = shortcuts.pop(name, None) is not None
            if existed:
                self._write(shortcuts)
            return existed
