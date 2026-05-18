"""Helpers for reading typed values out of environment variables.

These exist so that env-var parsing has one consistent failure mode across
the monorepo: missing / empty / unparseable values fall back to the
caller-supplied default rather than raising. Centralized here so that
adding a new env-var-driven knob does not require reinventing the
boundary-handling each time.
"""

import os


def parse_int_env(name: str, default: int) -> int:
    """Parse an int-valued env var; return ``default`` on missing/empty/invalid."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default
