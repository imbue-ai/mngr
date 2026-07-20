"""Harvesting ``witnesses`` markers from the test tree and joining them to the corpus.

``mngr specs matrix`` answers "which behavioral-spec units are witnessed by
tests, and how completely?". :func:`harvest_witness_links` runs an inner
``pytest --collect-only`` over the given test roots with a tiny stdlib-only
plugin (``witness_collection_plugin``) that dumps every ``witnesses`` marker as
JSONL; the pure functions below join those links to scanned units, classify
broken links, compute coverage, and render the matrix records.
"""

import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.mngr_specs.corpus import spec_unit_kind_record_value
from imbue.mngr_specs.data_types import SpecCoverage
from imbue.mngr_specs.data_types import SpecUnit
from imbue.mngr_specs.data_types import WitnessLink
from imbue.mngr_specs.errors import SpecWitnessCollectionError

# Dotted import path of the collection plugin, loaded into the inner pytest run
# with ``-p``. Hardcoded rather than imported so that importing this module
# never pulls pytest into the production ``mngr specs`` CLI (pytest is a dev-only
# dependency); the plugin itself is the only place that imports pytest.
_WITNESS_COLLECTION_PLUGIN_MODULE: Final[str] = "imbue.mngr_specs.witness_collection_plugin"

# Environment variable naming the JSONL file the collection plugin writes to.
# Read only by the inner pytest process (the plugin), set only by this module.
_WITNESSES_OUTPUT_PATH_ENV_VAR: Final[str] = "MNGR_SPECS_WITNESSES_OUTPUT_PATH"

# pytest exit codes that are not failures for a collect-only harvest: 0 means
# items were collected, 5 means none were (an empty test tree yields no links).
_OK_COLLECTION_EXIT_CODES: Final[tuple[int, ...]] = (0, 5)

# Two-threshold timeout for the inner collection (per the style guide): a hard
# ceiling meaning "definitely broken", and a lower "suspiciously slow" warning.
_COLLECTION_HARD_TIMEOUT_SECONDS: Final[float] = 300.0
_COLLECTION_WARN_THRESHOLD_SECONDS: Final[float] = 30.0

# How much of a failed pytest run's output to carry in the raised error.
_PYTEST_OUTPUT_EXCERPT_CHARACTER_COUNT: Final[int] = 4000


def harvest_witness_links(test_roots: Sequence[Path]) -> tuple[WitnessLink, ...]:
    """Run an inner ``pytest --collect-only`` over the test roots and return every ``witnesses`` link.

    Raises SpecWitnessCollectionError if pytest cannot collect: any exit code
    other than 0/5, a timeout, or unparseable plugin output. Needs the dev
    environment, since it shells out to pytest.
    """
    with tempfile.TemporaryDirectory() as temp_directory_name:
        output_path = Path(temp_directory_name) / "witnesses.jsonl"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            _WITNESS_COLLECTION_PLUGIN_MODULE,
            *(str(test_root) for test_root in test_roots),
        ]
        # Copy (never mutate) the ambient environment, adding the output-path var the plugin reads.
        subprocess_environment = {**os.environ, _WITNESSES_OUTPUT_PATH_ENV_VAR: str(output_path)}
        start_time = time.monotonic()
        with ConcurrencyGroup(name="mngr-specs-witness-collection") as concurrency_group:
            completed_collection = concurrency_group.run_process_to_completion(
                command,
                env=subprocess_environment,
                timeout=_COLLECTION_HARD_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
        if completed_collection.is_timed_out:
            raise SpecWitnessCollectionError(
                f"pytest --collect-only did not finish within {_COLLECTION_HARD_TIMEOUT_SECONDS:.0f}s over "
                f"test roots {[str(test_root) for test_root in test_roots]}"
            )
        elapsed_seconds = time.monotonic() - start_time
        if elapsed_seconds > _COLLECTION_WARN_THRESHOLD_SECONDS:
            logger.warning(
                "Collected witness markers in {:.1f}s (over the {:.0f}s warning threshold); test collection is slow",
                elapsed_seconds,
                _COLLECTION_WARN_THRESHOLD_SECONDS,
            )
        if completed_collection.returncode not in _OK_COLLECTION_EXIT_CODES:
            output_excerpt = (completed_collection.stderr or completed_collection.stdout)[
                -_PYTEST_OUTPUT_EXCERPT_CHARACTER_COUNT:
            ]
            raise SpecWitnessCollectionError(
                f"pytest --collect-only failed (exit code {completed_collection.returncode}) over test roots "
                f"{[str(test_root) for test_root in test_roots]}:\n{output_excerpt}"
            )
        return _parse_witness_links(output_path)


def _parse_witness_links(output_path: Path) -> tuple[WitnessLink, ...]:
    """Parse the plugin's JSONL output into links (a missing file means zero links were emitted)."""
    if not output_path.exists():
        return ()
    links: list[WitnessLink] = []
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SpecWitnessCollectionError(
                f"witness collection produced an unparseable output line: {line!r}"
            ) from exc
        links.append(WitnessLink.model_validate(record))
    return tuple(links)


@pure
def group_witness_links_by_coordinate(links: Sequence[WitnessLink]) -> dict[str, list[WitnessLink]]:
    """Group links carrying a coordinate by that coordinate, preserving collection order.

    Links with no coordinate (invalid marker usage) are not represented here;
    they surface via :func:`find_broken_witness_links`.
    """
    links_by_coordinate: dict[str, list[WitnessLink]] = {}
    for link in links:
        if link.coordinate is None:
            continue
        links_by_coordinate.setdefault(link.coordinate, []).append(link)
    return links_by_coordinate


@pure
def find_broken_witness_links(links: Sequence[WitnessLink], unit_coordinates: AbstractSet[str]) -> list[WitnessLink]:
    """Links that name no real unit -- invalid usage (no/non-string coordinate) or a dangling coordinate -- in order."""
    return [link for link in links if link.coordinate is None or link.coordinate not in unit_coordinates]


@pure
def compute_spec_coverage(links: Sequence[WitnessLink]) -> SpecCoverage:
    """Coverage for a unit given its links: full if any link is complete, partial if all are partial, else none."""
    if not links:
        return SpecCoverage.NONE
    if any(link.partial is None for link in links):
        return SpecCoverage.FULL
    return SpecCoverage.PARTIAL


@pure
def spec_coverage_record_value(coverage: SpecCoverage) -> str:
    """Render a coverage level as its JSONL record spelling."""
    match coverage:
        case SpecCoverage.FULL:
            return "full"
        case SpecCoverage.PARTIAL:
            return "partial"
        case SpecCoverage.NONE:
            return "none"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def render_matrix_record(unit: SpecUnit, links: Sequence[WitnessLink]) -> dict[str, Any]:
    """Render one ``mngr specs matrix`` JSONL record: the unit's identity plus its coverage and witnesses.

    ``links`` are exactly the links whose coordinate is this unit's, in
    collection order; each becomes a ``{"test", "partial"}`` witness object.
    """
    coverage = compute_spec_coverage(links)
    return {
        "coordinate": unit.coordinate,
        "kind": spec_unit_kind_record_value(unit.kind),
        "name": unit.name,
        "file": str(unit.file),
        "line": unit.line,
        "coverage": spec_coverage_record_value(coverage),
        "witnesses": [{"test": link.test, "partial": link.partial} for link in links],
    }


@pure
def render_broken_witness_link_diagnostic(link: WitnessLink) -> str:
    """A one-line stderr diagnostic for a broken link, keyed by the test's node id."""
    if link.coordinate is None:
        return f"{link.test}: witnesses marker is missing a string coordinate argument"
    return f"{link.test}: witnesses coordinate '{link.coordinate}' matches no spec unit"
