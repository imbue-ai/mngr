import os

import pytest

from imbue.mngr_vultr.config import VultrProviderConfig
from scripts.make_cli_docs import CONFIG_TABLES
from scripts.make_cli_docs import ConfigTable
from scripts.make_cli_docs import ConfigTableRow
from scripts.make_cli_docs import _validate_table_coverage
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


@pytest.mark.parametrize("table", CONFIG_TABLES, ids=lambda t: t.readme)
def test_config_table_covers_every_own_field(table: ConfigTable) -> None:
    # Every field declared on the config class must be either shown or explicitly excluded,
    # so a newly added field cannot silently vanish from the generated README.
    _validate_table_coverage(table)


def test_config_table_coverage_raises_on_undocumented_field() -> None:
    # A real own field (`backend`) with no row must fail the build.
    table = ConfigTable(
        readme="x/README.md",
        config_cls=VultrProviderConfig,
        field_header="Field",
        description_header="Description",
        rows=(ConfigTableRow("api_key", "`None`"),),
    )
    with pytest.raises(ValueError) as exc_info:
        _validate_table_coverage(table)
    assert "backend" in str(exc_info.value)
