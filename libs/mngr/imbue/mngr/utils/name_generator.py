from functools import cache
from pathlib import Path

import coolname
from coolname import RandomGenerator

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle

# Number of words to use when generating coolname-style names
_COOLNAME_WORD_COUNT = 3

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
    config = {
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


def generate_host_name(style: HostNameStyle) -> HostName:
    """Generate a random host name based on the specified style."""
    if style == HostNameStyle.COOLNAME:
        return HostName(coolname.generate_slug(_COOLNAME_WORD_COUNT))
    generator = _get_host_generator(style)
    name = generator.generate_slug()
    return HostName(name)
