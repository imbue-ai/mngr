"""A minimal collection plugin that dumps ``witnesses`` markers as JSONL.

Loaded into an inner ``pytest --collect-only`` run via ``-p`` by
``witnesses.harvest_witness_links``. It imports only the standard library and
pytest itself -- never any application code -- so collecting the test tree stays
isolated from (and cheap relative to) the ``mngr_specs`` package. On collection
finish it writes one JSON object per ``witnesses`` marker on every collected
item -- ``{"test", "coordinate", "partial"}`` -- to the path named by the
``MNGR_SPECS_WITNESSES_OUTPUT_PATH`` environment variable, recording marker
arguments faithfully (a missing or non-string coordinate becomes null) and
never crashing the collection run on odd marker usage.
"""

import json
import os
from pathlib import Path

import pytest


def _witness_records_for_item(item: pytest.Item) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for marker in item.iter_markers("witnesses"):
        raw_coordinate = marker.args[0] if marker.args else None
        coordinate = raw_coordinate if isinstance(raw_coordinate, str) else None
        raw_partial = marker.kwargs.get("partial")
        partial = raw_partial if isinstance(raw_partial, str) else None
        records.append({"test": item.nodeid, "coordinate": coordinate, "partial": partial})
    return records


def pytest_collection_finish(session: pytest.Session) -> None:
    output_path_name = os.environ.get("MNGR_SPECS_WITNESSES_OUTPUT_PATH")
    if output_path_name is None:
        return
    # Emit one JSONL line per witnesses marker across all collected items, in collection order.
    output_lines: list[str] = []
    for item in session.items:
        for record in _witness_records_for_item(item):
            output_lines.append(json.dumps(record))
    Path(output_path_name).write_text("".join(f"{line}\n" for line in output_lines), encoding="utf-8")
