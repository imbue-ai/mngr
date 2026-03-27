"""Integration tests for the name generator module."""

from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentNameStyle
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostNameStyle
from imbue.mng.utils.name_generator import _get_agent_generator
from imbue.mng.utils.name_generator import _get_host_generator
from imbue.mng.utils.name_generator import _get_resources_path
from imbue.mng.utils.name_generator import _load_wordlist
from imbue.mng.utils.name_generator import generate_agent_name
from imbue.mng.utils.name_generator import generate_host_name


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
    """Test that generate_agent_name generates unique names across multiple calls."""
    names = set()
    for _ in range(10):
        name = generate_agent_name(AgentNameStyle.ENGLISH)
        names.add(str(name))

    # With randomness, we expect most names to be unique
    # Allow for some duplicates due to randomness, but expect at least 5 unique names
    assert len(names) >= 5


def test_generate_host_name_generates_unique_names() -> None:
    """Test that generate_host_name generates unique names across multiple calls."""
    names = set()
    for _ in range(10):
        name = generate_host_name(HostNameStyle.ASTRONOMY)
        names.add(str(name))

    assert len(names) >= 5
