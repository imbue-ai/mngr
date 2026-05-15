"""Helpers for reading typed values out of environment variables.

These exist so that env-var parsing has one consistent failure mode across
the monorepo: missing / empty / unparseable values fall back to the
caller-supplied default rather than raising. Centralized here so that
adding a new env-var-driven knob does not require reinventing the
boundary-handling each time.
"""

import os
from typing import overload


@overload
def parse_int_env(name: str, default: int) -> int: ...


@overload
def parse_int_env(name: str, default: None = None) -> int | None: ...


def parse_int_env(name: str, default: int | None = None) -> int | None:
    """Parse an int-valued env var; return ``default`` on missing/empty/invalid.

    The default is ``None`` to support "is this env var set to a usable int?"
    queries; pass an explicit ``int`` default to get a non-Optional return.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default
