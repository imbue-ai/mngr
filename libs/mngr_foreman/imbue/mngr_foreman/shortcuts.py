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

from loguru import logger


class ShortcutStore:
    """File-backed map of shortcut name -> command string (read fresh per op)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()  # serialize this process's read-modify-writes

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001 - a corrupt/unreadable file must not wedge foreman
            logger.warning("Could not read shortcuts {}: {}", self._path, e)
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write(self, shortcuts: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(shortcuts, indent=2, sort_keys=True))
        tmp.replace(self._path)  # atomic

    def list(self) -> list[dict[str, str]]:
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
