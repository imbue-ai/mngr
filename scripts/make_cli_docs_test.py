import os

import pytest

from scripts.make_cli_docs import get_relative_link

# Importing make_cli_docs sets MNGR_LOAD_ALL_PLUGINS=1 process-wide at import time (it
# must, so doc generation loads every provider regardless of local config). Pop it here
# so that import side effect cannot leak into other tests sharing this xdist worker --
# notably main_test.py::test_create_plugin_manager_blocks_disabled_plugins, which calls
# create_plugin_manager() and would otherwise see plugin blocking silently skipped.
os.environ.pop("MNGR_LOAD_ALL_PLUGINS", None)


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
