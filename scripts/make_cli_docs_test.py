import pytest

from scripts.make_cli_docs import get_relative_link


def test_get_relative_link_resolves_known_command() -> None:
    # "list" and "create" are both PRIMARY_COMMANDS, so the link is same-directory.
    assert get_relative_link("list", "create") == "./create.md"


def test_get_relative_link_resolves_cross_category_command() -> None:
    # "file" is a SECONDARY_COMMAND, "create" is PRIMARY; link goes up and over.
    assert get_relative_link("file", "create") == "../primary/create.md"


def test_get_relative_link_raises_on_unresolvable_ref() -> None:
    # A see_also ref that is neither a known command nor a topic with a docs path
    # must fail loudly (so make_cli_docs.py --check catches the stale/typo'd ref)
    # instead of emitting a broken "mngr help <name>" markdown link.
    with pytest.raises(ValueError) as exc_info:
        get_relative_link("file", "definitely-not-a-real-command-or-topic")
    message = str(exc_info.value)
    assert "definitely-not-a-real-command-or-topic" in message
    assert "file" in message
