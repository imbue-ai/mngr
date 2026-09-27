"""One-time takeover of a ``mngr latchkey forward`` started before the ownership lock.

CLEANUP: delete this module, its test file, and the two calls into it once no
forward predating the ownership lock can still be running.

A forward from such a build holds no lock and publishes its pid to
``latchkey_forward.json`` instead, so nothing in the lock-based path can see it.
Left alone it would keep running beside a new lock-holding forward, putting two
``mngr observe`` producers on one events file. Everything needed to recognise
and clear one lives here rather than in the paths that outlive it, so removing
it later is a deletion rather than an untangling.

Recognising it means reading a process title, which is approximate: ``mngr``
overwrites its own title, and :meth:`psutil.Process.cmdline` hands back the
overwritten copy -- one fused string on macOS, re-split on spaces by psutil on
Linux. The tokens matched here contain no spaces, so they survive that; nothing
reads a *value* back out of a title.
"""

import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Final

import psutil
from loguru import logger

from imbue.mngr_latchkey.store import delete_forward_info
from imbue.mngr_latchkey.store import load_forward_info

# How far a pid's start time may sit after the record naming it and still be
# believed; covers only wall-clock disagreement between writer and reader.
_START_TIME_SKEW_ALLOWANCE_SECONDS: Final[float] = 5.0


def _cmdline_tokens(cmdline: list[str]) -> list[str]:
    """Return one token per argument, from either cmdline shape."""
    real_tokens = [tok for tok in cmdline if tok]
    if len(real_tokens) > 1:
        return real_tokens
    if not real_tokens:
        return []
    try:
        return shlex.split(real_tokens[0])
    except ValueError:
        return real_tokens[0].split()


def _looks_like_a_forward(cmdline: list[str]) -> bool:
    """Whether a process's cmdline is a ``mngr latchkey forward``.

    ``mngr`` is matched as a whole path component, never as a substring of
    ``manager`` or ``mngr-foo``.
    """
    tokens = _cmdline_tokens(cmdline)
    for index, token in enumerate(tokens):
        if token == "mngr" or token.endswith("/mngr"):
            remainder = tokens[index + 1 :]
            return "latchkey" in remainder and "forward" in remainder
    return False


def pre_lock_forward_pid(plugin_data_dir: Path) -> int | None:
    """Return the pid of a live pre-lock forward recorded here, if there is one.

    The record outlives the process it names, so the pid is paired with the
    ``started_at`` every forward wrote: a forward is already running when it
    writes that, while a pid handed to something else afterwards belongs to a
    process younger than the record.
    """
    info = load_forward_info(plugin_data_dir)
    if info is None:
        return None
    try:
        process = psutil.Process(info.pid)
        process_started_at = process.create_time()
        cmdline = process.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    if process_started_at > info.started_at.timestamp() + _START_TIME_SKEW_ALLOWANCE_SECONDS:
        return None
    if not _looks_like_a_forward(cmdline):
        return None
    return info.pid


def migrate_pre_lock_forward(plugin_data_dir: Path, terminate: Callable[[int], None]) -> None:
    """Clear a forward that predates the ownership lock, so a new one can take over.

    A no-op once none can be running, which is what makes this module deletable.
    """
    pid = pre_lock_forward_pid(plugin_data_dir)
    if pid is not None:
        logger.info("Replacing a mngr latchkey forward (pid={}) that predates the ownership lock", pid)
        terminate(pid)
    delete_forward_info(plugin_data_dir)
