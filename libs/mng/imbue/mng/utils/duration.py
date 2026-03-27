import re

from imbue.imbue_common.pure import pure
from imbue.mng.errors import UserInputError

_DURATION_PATTERN = re.compile(
    r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?$",
    re.IGNORECASE,
)


# FIXME: we probably should just use a library for this. In particular, we should convert *all* places where we accept durations to use this function for converting from strings into a number of seconds
#  and then we can be sure that we're consistent about how we parse durations across the board.  This means scanning across *all* of our CLI commands to find any arguments that refer to durations.
#  Also, we probably want to support every sensible format, like "5s", "5 seconds", "5 sec", "5m", "5 minutes", "5 min", etc.
#  As part of this fix, go find a nice 3rd-party library, and then convert this function to use that instead of the ad-hoc approach here
#  to be clear--all of our *internal* durations should be in seconds (float), but we should be flexible about the durations we accept from users (e.g. in config files, command line arguments, etc) and allow those to be in any sane form.
@pure
def parse_duration_to_seconds(duration_str: str) -> float:
    """Parse a human-readable duration string into seconds.

    Supports plain integers (treated as seconds) and combinations of
    days (d), hours (h), minutes (m), seconds (s).
    Examples: '300', '7d', '24h', '30m', '1h30m', '90s', '1d12h'.
    """
    stripped = duration_str.strip()
    if not stripped:
        raise UserInputError(f"Invalid duration: '{duration_str}' (empty string)")

    # Plain integer is treated as seconds
    try:
        plain_seconds = int(stripped)
        if plain_seconds <= 0:
            raise UserInputError(f"Invalid duration: '{duration_str}'. Duration must be greater than zero.")
        return float(plain_seconds)
    except ValueError:
        pass

    match = _DURATION_PATTERN.match(stripped)
    if match is None or match.group(0) == "":
        raise UserInputError(
            f"Invalid duration: '{duration_str}'. Expected format like '300', '7d', '24h', '30m', '90s', '1h30m', '1d12h'."
        )

    days = int(match.group(1)) if match.group(1) else 0
    hours = int(match.group(2)) if match.group(2) else 0
    minutes = int(match.group(3)) if match.group(3) else 0
    seconds = int(match.group(4)) if match.group(4) else 0

    total_seconds = float(days * 86400 + hours * 3600 + minutes * 60 + seconds)

    if total_seconds == 0.0:
        raise UserInputError(f"Invalid duration: '{duration_str}'. Duration must be greater than zero.")

    return total_seconds
