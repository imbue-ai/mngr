import ctypes
import os
from collections.abc import Mapping
from typing import Final

from loguru import logger

# glibc's mallopt parameter for the most arenas the allocator may create (malloc.h).
_M_ARENA_MAX: Final[int] = -8
# A burst of threads grows one arena per thread (up to eight per core), and each keeps its
# pages after its threads exit. Four, not fewer: fewer arenas mean more threads contend
# for each one, and on a measured allocation-heavy service two arenas cost 55% more CPU
# per MB allocated against 9% for four.
MNGR_MALLOC_ARENA_MAX: Final[int] = 4
_GLIBC_CONFSTR_NAME: Final[str] = "CS_GNU_LIBC_VERSION"
_GLIBC_LIBRARY_NAME: Final[str] = "libc.so.6"
_GLIBC_ARENA_MAX_TUNABLE: Final[str] = "glibc.malloc.arena_max"


def is_glibc() -> bool:
    """Whether this process runs on glibc.

    musl names the glibc version setting too but does not answer it, so the name alone
    does not tell the two apart.
    """
    if _GLIBC_CONFSTR_NAME not in os.confstr_names:
        return False
    try:
        libc_version = os.confstr(_GLIBC_CONFSTR_NAME)
    except OSError as e:
        logger.trace("The C library does not report a glibc version, so it is not glibc: {}", e)
        return False
    return libc_version is not None and libc_version.startswith("glibc")


def _is_malloc_arena_max_set_by_environment(environ: Mapping[str, str]) -> bool:
    """Whether the environment already sets glibc's arena limit, which an operator's choice should keep."""
    return "MALLOC_ARENA_MAX" in environ or _GLIBC_ARENA_MAX_TUNABLE in environ.get("GLIBC_TUNABLES", "")


def cap_malloc_arenas() -> bool:
    """Cap this process's glibc malloc arenas at ``MNGR_MALLOC_ARENA_MAX``; whether the cap was set.

    A no-op off glibc (macOS and musl have no arenas to cap) and when the environment
    already sets the limit. Only arenas created afterwards are bounded, so call it
    before the process starts threads. It changes this process alone: unlike the
    environment variable, it is not inherited by the agents mngr launches.
    """
    if not is_glibc():
        return False
    if _is_malloc_arena_max_set_by_environment(os.environ):
        return False
    libc = ctypes.CDLL(_GLIBC_LIBRARY_NAME)
    return libc.mallopt(_M_ARENA_MAX, MNGR_MALLOC_ARENA_MAX) == 1
