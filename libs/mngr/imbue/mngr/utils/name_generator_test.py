"""Unit tests for the name generator module."""

import random

from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.utils.name_generator import _get_agent_generator
from imbue.mngr.utils.name_generator import _get_host_generator
from imbue.mngr.utils.name_generator import _get_resources_path
from imbue.mngr.utils.name_generator import _load_wordlist
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.mngr.utils.name_generator import generate_host_name


def test_get_resources_path_returns_valid_path() -> None:
    """Test that _get_resources_path returns a path to name_lists directory."""
    resources_path = _get_resources_path()

    assert resources_path.exists()
    assert resources_path.name == "name_lists"
    assert (resources_path / "agent").exists()
    assert (resources_path / "host").exists()


def test_load_wordlist_for_agent_english() -> None:
    """Test loading agent wordlist for English style."""
    words = _load_wordlist("agent", "english")

    assert len(words) > 0
    for word in words:
        assert isinstance(word, str)
        assert len(word) > 0


def test_load_wordlist_for_agent_fantasy() -> None:
    """Test loading agent wordlist for fantasy style."""
    words = _load_wordlist("agent", "fantasy")

    assert len(words) > 0
    for word in words:
        assert isinstance(word, str)
        assert len(word) > 0


def test_get_agent_generator_returns_generator() -> None:
    """Test that _get_agent_generator returns a RandomGenerator."""
    generator = _get_agent_generator(AgentNameStyle.ENGLISH)

    assert generator is not None
    # Generate a name to verify it works
    name = generator.generate_slug()
    assert isinstance(name, str)
    assert len(name) > 0

    # and that it is cached
    assert generator is _get_agent_generator(AgentNameStyle.ENGLISH)


def test_get_host_generator_returns_generator() -> None:
    """Test that _get_host_generator returns a RandomGenerator."""
    generator = _get_host_generator(HostNameStyle.ASTRONOMY)

    assert generator is not None
    name = generator.generate_slug()
    assert isinstance(name, str)
    assert len(name) > 0

    # and that it is cached
    assert generator is _get_host_generator(HostNameStyle.ASTRONOMY)


def test_generate_agent_name_english_returns_agent_name() -> None:
    """Test generating agent name with English style."""
    name = generate_agent_name(AgentNameStyle.ENGLISH)

    assert isinstance(name, AgentName)
    assert len(name) > 0


def test_generate_agent_name() -> None:
    """Test generating agent name with all styles."""
    for name_style in AgentNameStyle.__members__.values():
        name = generate_agent_name(name_style)

        assert isinstance(name, AgentName)
        assert len(name) > 0


def test_generate_host_name() -> None:
    """Test generating host name with all styles."""
    for name_style in HostNameStyle.__members__.values():
        name = generate_host_name(name_style)

        assert isinstance(name, HostName)
        assert len(name) > 0


def test_generate_agent_name_generates_unique_names() -> None:
    """generate_agent_name yields distinct names across calls under a fixed RNG seed.

    The English agent style draws from a large first/last-name cartesian product, so a
    correct generator produces 10 distinct names from 10 draws. Seeding the global RNG
    (and restoring it afterwards) makes this exact and deterministic rather than relying
    on a probabilistic `>= 5` threshold that could flake.
    """
    # Warm the cached generator before seeding so the seed governs only the draws, not
    # the (RNG-consuming) generator construction -- otherwise the result would depend on
    # whether an earlier test already populated the cache.
    _get_agent_generator(AgentNameStyle.ENGLISH)
    saved_state = random.getstate()
    try:
        random.seed(12345)
        names = {str(generate_agent_name(AgentNameStyle.ENGLISH)) for _ in range(10)}
    finally:
        random.setstate(saved_state)

    assert len(names) == 10


def test_generate_host_name_generates_unique_names() -> None:
    """generate_host_name yields mostly-distinct names across calls under a fixed RNG seed.

    The astronomy host style draws from a single wordlist, so 10 draws collide more often
    than the agent cartesian product; with seed 12345 exactly 9 of the 10 draws are unique.
    Seeding (and restoring) the RNG pins this exactly instead of using a loose threshold.
    """
    # Warm the cached generator before seeding so the seed governs only the draws, not
    # the (RNG-consuming) generator construction -- otherwise the result would depend on
    # whether an earlier test already populated the cache.
    _get_host_generator(HostNameStyle.ASTRONOMY)
    saved_state = random.getstate()
    try:
        random.seed(12345)
        names = {str(generate_host_name(HostNameStyle.ASTRONOMY)) for _ in range(10)}
    finally:
        random.setstate(saved_state)

    assert len(names) == 9
