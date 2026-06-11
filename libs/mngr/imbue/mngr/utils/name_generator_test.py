"""Unit tests for the name generator module."""

from collections.abc import Callable

import pytest

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


# Draw many names and require a healthy number of distinct ones. This pins the observable
# behavior -- the generator actually varies its output over a large space -- without seeding
# the global RNG or asserting an exact unique count (which would couple the test to the
# wordlist size and the generator's internal draw order, the "fails for the wrong reason"
# anti-pattern). The threshold is set so a correct generator essentially never flakes: the
# smallest space here is the 40-word astronomy host list, and the chance of 50 draws yielding
# fewer than 10 distinct values is bounded by C(40, 9) * (9/40)**50 ~= 1e-24. It also
# tolerates wordlist edits -- it holds for any list of more than ~15 words.
_DRAW_COUNT = 50
_MIN_DISTINCT = 10


@pytest.mark.parametrize(
    "make_name",
    [
        pytest.param(lambda: generate_agent_name(AgentNameStyle.ENGLISH), id="agent_english"),
        pytest.param(lambda: generate_host_name(HostNameStyle.ASTRONOMY), id="host_astronomy"),
    ],
)
def test_name_generator_produces_varied_names(make_name: Callable[[], object]) -> None:
    names = {str(make_name()) for _ in range(_DRAW_COUNT)}

    assert len(names) >= _MIN_DISTINCT
