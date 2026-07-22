"""Foreman-local record of which agents the user has parked ("backburner").

Kept OUT of the agents themselves -- no per-agent host round-trip to set an mngr
label (that was slow and could hang on an unresponsive host). Just a JSON set of
agent IDs on the foreman box: tagged onto each card at snapshot time (same cadence
as the agent list) and toggled by a fast local write.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from loguru import logger


class BackburnerStore:
    """Thread-safe, file-backed set of parked agent IDs."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._ids: set[str] = self._load()

    def _load(self) -> set[str]:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return set()
        except Exception as e:  # noqa: BLE001 - a corrupt/unreadable file must not wedge foreman
            logger.warning("Could not read backburner state {}: {}", self._path, e)
            return set()
        return {str(x) for x in data} if isinstance(data, list) else set()

    def _save(self) -> None:
        # Atomic write so a crash mid-write can't corrupt the set.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(self._ids)))
        tmp.replace(self._path)

    def is_backburner(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._ids

    def set_parked(self, agent_id: str, on: bool) -> None:
        with self._lock:
            if on:
                self._ids.add(agent_id)
            else:
                self._ids.discard(agent_id)
            self._save()
