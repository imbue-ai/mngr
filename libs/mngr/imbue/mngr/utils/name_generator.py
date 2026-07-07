from functools import cache
from pathlib import Path
from typing import Final

import coolname
from coolname import CoolnameConfigT
from coolname import RandomGenerator

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import MAX_HOST_NAME_LENGTH

# Number of words to use when generating coolname-style names
_COOLNAME_WORD_COUNT = 3

# How many times to re-roll a generated host name that exceeds the length cap before truncating.
_MAX_HOST_NAME_LENGTH_ROLLS: Final[int] = 10

# Fallback used when callers that build <name>-<uuid> directories don't have a user-supplied name.
GENERIC_AGENT_NAME_HINT: Final[str] = "agent"


@pure
def pick_agent_name_hint(*candidates: str | None) -> str:
    """Return the first non-empty candidate, falling back to `GENERIC_AGENT_NAME_HINT`.

    For building `<name>-<uuid>` directory paths where the exact name only matters
    as a human-readable prefix (not for addressing), so the return type is a plain
    `str` and values are not validated as `AgentName`.
    """
    for candidate in candidates:
        if candidate:
            return candidate
    return GENERIC_AGENT_NAME_HINT


# Styles that use first_name + last_name format
_STYLES_WITH_LAST_NAMES: frozenset[AgentNameStyle] = frozenset(
    {AgentNameStyle.ENGLISH, AgentNameStyle.FANTASY, AgentNameStyle.SCIFI}
)


@pure
def _get_resources_path() -> Path:
    """Get the path to the resources directory."""
    return Path(__file__).parent.parent / "resources" / "data" / "name_lists"


def _load_wordlist(category: str, style: str) -> list[str]:
    """Load a wordlist from a txt file, returning a flat list of strings."""
    wordlist_path = _get_resources_path() / category / f"{style}.txt"
    words: list[str] = []
    for line in wordlist_path.read_text().splitlines():
        stripped_line = line.strip()
        if stripped_line and not stripped_line.startswith("#"):
            words.append(stripped_line)
    return words


@cache
def _get_agent_generator(style: AgentNameStyle) -> RandomGenerator:
    """Get a cached RandomGenerator for the given agent name style."""
    style_name = style.value.lower()
    first_names = _load_wordlist("agent", style_name)

    config: CoolnameConfigT
    if style in _STYLES_WITH_LAST_NAMES:
        last_names = _load_wordlist("agent", f"{style_name}_last")
        config = {
            "all": {
                "type": "cartesian",
                "lists": ["first", "last"],
            },
            "first": {
                "type": "words",
                "words": first_names,
            },
            "last": {
                "type": "words",
                "words": last_names,
            },
        }
    else:
        config = {
            "all": {
                "type": "words",
                "words": first_names,
            },
        }
    return RandomGenerator(config)


@cache
def _get_host_generator(style: HostNameStyle) -> RandomGenerator:
    """Get a cached RandomGenerator for the given host name style."""
    style_name = style.value.lower()
    words = _load_wordlist("host", style_name)
    config: CoolnameConfigT = {
        "all": {
            "type": "words",
            "words": words,
        },
    }
    return RandomGenerator(config)


def generate_agent_name(style: AgentNameStyle) -> AgentName:
    """Generate a random agent name based on the specified style."""
    if style == AgentNameStyle.COOLNAME:
        return AgentName(coolname.generate_slug(_COOLNAME_WORD_COUNT))
    generator = _get_agent_generator(style)
    if style in _STYLES_WITH_LAST_NAMES:
        # Use underscore separator for firstname_lastname format
        name = "-".join(generator.generate())
    else:
        name = generator.generate_slug()
    return AgentName(name)


def _generate_host_name_slug(style: HostNameStyle) -> str:
    """Generate a single random host-name slug for the given style (length unchecked)."""
    if style == HostNameStyle.COOLNAME:
        return coolname.generate_slug(_COOLNAME_WORD_COUNT)
    return _get_host_generator(style).generate_slug()


def generate_host_name(style: HostNameStyle) -> HostName:
    """Generate a random host name based on the specified style, within the length cap.

    Generated slugs (especially coolname's) can exceed ``MAX_HOST_NAME_LENGTH``.
    Re-roll a bounded number of times to keep a clean whole-word name; if every
    roll is too long, truncate the last one (stripping any trailing separator).
    """
    candidate = _generate_host_name_slug(style)
    for _ in range(_MAX_HOST_NAME_LENGTH_ROLLS):
        if len(candidate) <= MAX_HOST_NAME_LENGTH:
            return HostName(candidate)
        candidate = _generate_host_name_slug(style)
    return HostName(candidate[:MAX_HOST_NAME_LENGTH].rstrip("-_"))
