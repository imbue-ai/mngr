"""mngr_donate plugin entry point.

Provides the ``mngr donate`` command: spend spare Claude capacity (as reported by
the ``mngr usage`` snapshot) on a donation skill. Kept separate from ``mngr_usage``
because measuring usage and donating idle quota are orthogonal capabilities; this
plugin depends on ``imbue-mngr-usage`` (installing donate pulls usage in) and reads
its ``usage`` plugin config + snapshot API at runtime.
"""

from __future__ import annotations

from collections.abc import Sequence

import click

from imbue.mngr import hookimpl
from imbue.mngr_donate.donate import donate


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register the `mngr donate` command."""
    return [donate]
