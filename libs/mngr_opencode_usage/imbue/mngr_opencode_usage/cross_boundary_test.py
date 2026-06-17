"""Guard the TS-writer <-> Python-reader source-name seam.

The writer (`.ts`) and the reader (`plugin.py`) agree on the source name only by
hand-synced string literals across the language boundary. A rename on one side
would silently disable usage tracking, so pin the invariant here.
"""

from __future__ import annotations

import importlib.resources

from imbue.mngr_opencode_usage import resources as _resources
from imbue.mngr_opencode_usage.plugin import _OPENCODE_USAGE_SOURCE_NAME
from imbue.mngr_opencode_usage.plugin import _USAGE_PLUGIN_FILENAME


def test_writer_ts_emits_the_source_the_reader_claims() -> None:
    writer_ts = importlib.resources.files(_resources).joinpath(_USAGE_PLUGIN_FILENAME).read_text()
    # The writer emits `source: "<name>/usage"`; the reader strips "/usage" and
    # claims "<name>". Assert the writer's literal matches the reader's constant.
    assert f'"{_OPENCODE_USAGE_SOURCE_NAME}/usage"' in writer_ts
