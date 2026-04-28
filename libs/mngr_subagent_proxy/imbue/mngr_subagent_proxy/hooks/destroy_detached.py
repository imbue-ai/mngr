"""Fire-and-forget detached destroy of an mngr agent.

Lives in its own module (not ``hooks/mngr_api.py``) so importing it
does not pull in ``imbue.mngr.main`` — which loads plugin entry points
and would create a circular import when called from ``plugin.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from loguru import logger

# DI signature for ``destroy_agent_detached``. Lives with the function so
# every caller (plugin.py, hooks/rewrite.py, hooks/reap.py) imports the same
# alias and tests have a single name to inject against.
DestroyAgentDetachedCallable = Callable[[str, Path], None]


def destroy_agent_detached(target_name: str, log_path: Path) -> None:
    """Fire-and-forget detached destroy using a child Python process.

    Spawns a ``python -m imbue.mngr_subagent_proxy.hooks.destroy_worker``
    child whose lifetime is independent of the caller. Stderr is appended
    to ``log_path``.
    """
    log_handle = None
    try:
        log_handle = log_path.open("ab")
    except OSError as e:
        logger.warning("destroy_agent_detached: failed to open log {}: {}", log_path, e)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "imbue.mngr_subagent_proxy.hooks.destroy_worker", target_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_handle if log_handle is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        logger.warning("destroy_agent_detached: failed to launch destroy worker: {}", e)
    finally:
        if log_handle is not None:
            log_handle.close()
