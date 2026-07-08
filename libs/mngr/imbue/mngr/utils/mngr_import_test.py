import click

from imbue import mngr
from imbue.mngr import main


def test_mngr_package_imports_and_exposes_cli_entrypoint() -> None:
    """Importing imbue.mngr should succeed and expose the CLI entrypoint.

    Asserting on a concrete, load-bearing attribute (the `cli` group wired up as
    the `mngr` console script in pyproject.toml) catches a broken package layout
    or a missing/renamed entrypoint, which a bare `assert mngr` (a module object
    is always truthy) would not.
    """
    assert mngr.__name__ == "imbue.mngr"
    assert isinstance(main.cli, click.Command)
