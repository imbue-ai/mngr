"""Read and validate the host machine's IANA timezone.

Backs the ``GET /api/v1/timezone`` route: workspace agents (via the latchkey
gateway) ask the desktop client for the user's local timezone instead of the
desktop client pushing it into each workspace at create time. Lives in a low
module (no imports from ``api_v1``) so the route handler and any future caller
can share it without an import cycle.
"""

from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

import tzlocal
from loguru import logger


def validate_iana_timezone(raw_timezone: object) -> str:
    """Validate a candidate IANA timezone name, or return ``""``.

    A missing or unrecognized value is treated as "unknown" (returns ``""``)
    rather than raising -- callers fall back to the host clock. Validating
    against the system tz database keeps anything unexpected from being
    reported as a real timezone.
    """
    stripped = str(raw_timezone).strip() if raw_timezone is not None else ""
    if not stripped:
        return ""
    try:
        ZoneInfo(stripped)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Ignoring unrecognized IANA timezone {!r}; callers will fall back to the host clock.", stripped)
        return ""
    return stripped


def read_host_timezone() -> str:
    """Return this machine's IANA timezone name, or ``""`` when undeterminable.

    ``tzlocal`` resolves the OS-level timezone (e.g. macOS system settings). Its
    failure modes -- an undeterminable or conflicting OS config
    (``ZoneInfoNotFoundError``, a ``LookupError``), malformed config contents
    (``ValueError``), or an unreadable config file (``OSError``) -- are logged
    and collapsed to ``""`` so the API route never errors over an odd host
    setup; the result is re-validated so callers always get a real IANA name
    or ``""``.
    """
    try:
        name = tzlocal.get_localzone_name()
    except (LookupError, ValueError, OSError) as exc:
        logger.warning("Could not determine the host timezone: {}", exc)
        return ""
    return validate_iana_timezone(name)
