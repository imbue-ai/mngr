from collections.abc import Sequence
from typing import Any

import pluggy

from imbue.mngr.config.data_types import MngrContext

hookspec = pluggy.HookspecMarker("mngr")


@hookspec
def kanpan_data_sources(mngr_ctx: MngrContext) -> Sequence[Any] | None:
    """Register data sources for kanpan board refresh.

    Each data source must implement the KanpanDataSource protocol
    (defined in imbue.mngr_kanpan.data_source). Data sources produce
    typed fields that become columns on the kanpan board.

    Return a sequence of data source instances, or None if not contributing any.
    """
