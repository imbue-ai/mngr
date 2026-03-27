import shlex
from io import StringIO
from pathlib import Path

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


@pure
def build_source_env_shell_commands(
    host_env_path: Path,
    agent_env_path: Path,
) -> list[str]:
    """Build shell commands that source host and agent env files.

    Returns a list of shell commands that:
    1. Set 'set -a' to auto-export all sourced variables
    2. Source host env if it exists (host env first)
    3. Source agent env if it exists (agent can override host)
    4. Restore with 'set +a'

    The caller is responsible for joining these appropriately.
    """
    return [
        "set -a",
        f"[ -f {shlex.quote(str(host_env_path))} ] && . {shlex.quote(str(host_env_path))} || true",
        f"[ -f {shlex.quote(str(agent_env_path))} ] && . {shlex.quote(str(agent_env_path))} || true",
        "set +a",
    ]
