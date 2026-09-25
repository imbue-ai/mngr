import re
import shlex
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Final

from dotenv import dotenv_values

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError

_TRUTHY_VALUES = frozenset(("1", "true", "yes"))

# Prefix used for test environments across providers (e.g. Modal environment
# names). Defined here rather than in utils/testing.py because non-test code
# paths (e.g. mngr_modal.backend) need to recognise these at runtime, and
# utils/testing.py is excluded from the runtime wheel and imports pytest.
TEST_ENV_PREFIX: Final[str] = "mngr_test-"

# Matches test environment names: mngr_test-YYYY-MM-DD-HH-MM-SS[-user_id].
TEST_ENV_PATTERN: Final[re.Pattern[str]] = re.compile(r"^mngr_test-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")

# Matches the per-test prefix produced by the autouse ``mngr_test_prefix``
# fixture: ``mngr_<32 hex chars>-`` (from ``uuid4().hex``). For example:
# ``mngr_22921e597952421296c8973d922f2eb3-docker-state-...``. Used by the test
# session cleanup to recognise leaked test resources (e.g. docker containers)
# whose names carry this prefix.
TEST_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^mngr_[0-9a-f]{32}-")

# Random suffix ending every test user id, so that sessions started in the same
# second under the same agent name (TMR mappers share a long name prefix) still
# get distinct ids, and so that a provider truncating the id from the right
# cannot strip the part that makes it unique.
TEST_USER_ID_RANDOM_SUFFIX_LENGTH: Final[int] = 8

_TEST_USER_ID_TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d-%H-%M-%S"


class InvalidTestUserIdBudgetError(MngrError, ValueError):
    """Raised when a test user id's maximum length cannot even hold its timestamp and random suffix."""

    ...


@pure
def build_test_user_id(timestamp: datetime, agent_name: str | None, random_suffix: str, max_length: int) -> str:
    """Build a ``YYYY-MM-DD-HH-MM-SS[-<agent name>]-<random suffix>`` test user id of at most ``max_length``.

    The timestamp comes first so the id matches ``TEST_ENV_PATTERN`` once
    prefixed, and the random suffix comes last so it survives any truncation
    the agent name is cut to make room for.

    Raises InvalidTestUserIdBudgetError when ``max_length`` cannot hold the timestamp
    and the suffix.
    """
    stamp = timestamp.strftime(_TEST_USER_ID_TIMESTAMP_FORMAT)
    shortest_id = f"{stamp}-{random_suffix}"
    if len(shortest_id) > max_length:
        raise InvalidTestUserIdBudgetError(
            f"A test user id needs at least {len(shortest_id)} characters for its timestamp and suffix, "
            f"but at most {max_length} are allowed"
        )
    name_budget = max_length - len(shortest_id) - 1
    name_part = (agent_name or "")[:name_budget].rstrip("-")
    if not name_part:
        return shortest_id
    return f"{stamp}-{name_part}-{random_suffix}"


@pure
def looks_like_mngr_test_container_name(container_name: str) -> bool:
    """Whether a container name starts with the autouse per-test prefix (mngr_<hex32>-)."""
    return TEST_PREFIX_PATTERN.match(container_name) is not None


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
