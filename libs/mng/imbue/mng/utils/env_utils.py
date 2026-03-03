from io import StringIO

from dotenv import dotenv_values

from imbue.imbue_common.pure import pure

_TRUTHY_VALUES = frozenset(("1", "true", "yes"))


@pure
def parse_bool_env(value: str) -> bool:
    """Parse a string as a boolean, as used by environment variables.

    Recognizes "1", "true", "yes" (case-insensitive) as True.
    Everything else (including empty string) is False.
    """
    return value.lower() in _TRUTHY_VALUES


@pure
def parse_env_file(content: str) -> dict[str, str]:
    """Parse an environment file into a dict."""
    raw = dotenv_values(stream=StringIO(content))
    return {k: v for k, v in raw.items() if v is not None}
