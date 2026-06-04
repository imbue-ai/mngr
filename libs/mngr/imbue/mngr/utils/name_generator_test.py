"""Unit tests for the name generator module."""

from collections.abc import Callable

import pytest

from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.utils.name_generator import _COOLNAME_WORD_COUNT
from imbue.mngr.utils.name_generator import _STYLES_WITH_LAST_NAMES
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


def test_get_agent_generator_cache_is_keyed_by_style() -> None:
    """Distinct styles must yield distinct cached generators (a cache key that ignored the
    style would return the same generator for every style and silently break name styling)."""
    english = _get_agent_generator(AgentNameStyle.ENGLISH)
    animals = _get_agent_generator(AgentNameStyle.ANIMALS)

    assert english is not animals


def test_get_host_generator_returns_generator() -> None:
    """Test that _get_host_generator returns a RandomGenerator."""
    generator = _get_host_generator(HostNameStyle.ASTRONOMY)

    assert generator is not None
    name = generator.generate_slug()
    assert isinstance(name, str)
    assert len(name) > 0

    # and that it is cached
    assert generator is _get_host_generator(HostNameStyle.ASTRONOMY)


def test_get_host_generator_cache_is_keyed_by_style() -> None:
    """Distinct host styles must yield distinct cached generators."""
    astronomy = _get_host_generator(HostNameStyle.ASTRONOMY)
    cities = _get_host_generator(HostNameStyle.CITIES)

    assert astronomy is not cities


def test_generate_agent_name_english_is_first_last_from_wordlists() -> None:
    """English is a first+last style, so the name must be `first-last` with each part drawn
    from the corresponding english / english_last wordlists."""
    first_names = set(_load_wordlist("agent", "english"))
    last_names = set(_load_wordlist("agent", "english_last"))

    name = generate_agent_name(AgentNameStyle.ENGLISH)

    assert isinstance(name, AgentName)
    parts = str(name).split("-")
    assert len(parts) == 2, f"expected first-last, got {name!r}"
    first, last = parts
    assert first in first_names
    assert last in last_names


def test_generate_agent_name() -> None:
    """Every agent style must produce a name composed of words drawn from its own wordlist(s).

    Last-name styles produce `first-last`; COOLNAME produces a fixed-length coolname slug;
    all other styles produce a single word from their wordlist.
    """
    for name_style in AgentNameStyle.__members__.values():
        name = generate_agent_name(name_style)

        assert isinstance(name, AgentName)
        parts = str(name).split("-")

        if name_style == AgentNameStyle.COOLNAME:
            # coolname.generate_slug(_COOLNAME_WORD_COUNT) joins that many *concepts*,
            # but some concepts expand to multi-token phrases (e.g. "...-of-..."), so the
            # hyphen-token count is _COOLNAME_WORD_COUNT or more -- asserting exact equality
            # would flake ~1/3 of the time. We pin the floor and that every token is a
            # non-empty lowercase word, which still catches an empty/single-word regression.
            assert len(parts) >= _COOLNAME_WORD_COUNT
            for part in parts:
                assert part.isalpha() and part.islower()
            continue

        style_name = name_style.value.lower()
        first_names = set(_load_wordlist("agent", style_name))

        if name_style in _STYLES_WITH_LAST_NAMES:
            last_names = set(_load_wordlist("agent", f"{style_name}_last"))
            assert len(parts) == 2, f"{name_style}: expected first-last, got {name!r}"
            assert parts[0] in first_names
            assert parts[1] in last_names
        else:
            assert len(parts) == 1, f"{name_style}: expected single word, got {name!r}"
            assert parts[0] in first_names


def test_generate_host_name() -> None:
    """Every host style must produce a name drawn from its own wordlist.

    COOLNAME produces a fixed-length slug; every other host style is a single word.
    """
    for name_style in HostNameStyle.__members__.values():
        name = generate_host_name(name_style)

        assert isinstance(name, HostName)
        parts = str(name).split("-")

        if name_style == HostNameStyle.COOLNAME:
            # See test_generate_agent_name: coolname concepts can expand to multi-token
            # phrases, so the hyphen-token count is a lower bound, not an exact match.
            assert len(parts) >= _COOLNAME_WORD_COUNT
            for part in parts:
                assert part.isalpha() and part.islower()
            continue

        words = set(_load_wordlist("host", name_style.value.lower()))
        assert len(parts) == 1, f"{name_style}: expected single word, got {name!r}"
        assert parts[0] in words


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
