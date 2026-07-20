"""The spec-anchored test-mapreduce recipe: fanning out behavioral-spec units.

Sibling of :mod:`imbue.mngr_tmr.recipe` with the spec unit replacing the
docstring as the scope anchor. Discovery scans a behavioral-spec corpus
(``imbue.mngr_specs``) and groups its units into one task per ``.feature``
file; mappers create or update the tests witnessing those units. The
framework (``imbue.mngr_mapreduce``) handles agent launching, polling,
output extraction, and CLI plumbing.

Fan-out granularity is deliberately a local decision: outcomes are keyed by
unit coordinate, never by task, so re-partitioning (per unit, per area, per
Rule) only changes the grouping in ``discover_spec_tasks`` and the task-id
scheme.
"""

from pathlib import Path

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_specs.corpus import scan_corpus
from imbue.mngr_specs.corpus import spec_unit_matches_area
from imbue.mngr_specs.corpus import spec_unit_matches_tag
from imbue.mngr_specs.data_types import SpecUnit
from imbue.mngr_specs.data_types import SpecUnitKind
from imbue.mngr_specs.data_types import SpecViolation


class SpecCorpusInvalidError(MngrError, RuntimeError):
    """Raised when the behavioral-spec corpus has language violations at discovery time.

    Discovery fail-fasts on an invalid corpus: a fleet anchored to a broken
    corpus would spend agents on garbage, and the corpus cannot change during
    a run (it is read-only to the whole pipeline).
    """

    ...


class NoSpecUnitsError(MngrError, RuntimeError):
    """Raised when discovery selects zero spec units (empty corpus or over-narrow filters)."""

    ...


@pure
def _format_corpus_violation(violation: SpecViolation) -> str:
    location = str(violation.file) if violation.line is None else f"{violation.file}:{violation.line}"
    return f"{location}: {violation.message}"


@pure
def _spec_unit_passes_filters(
    unit: SpecUnit,
    scan_root: Path,
    area: str | None,
    tag: str | None,
    unit_kind: SpecUnitKind | None,
) -> bool:
    """AND-compose the selection filters; the matching semantics are layer 1's."""
    if area is not None and not spec_unit_matches_area(unit, area, scan_root):
        return False
    if tag is not None and not spec_unit_matches_tag(unit, tag):
        return False
    if unit_kind is not None and unit.kind != unit_kind:
        return False
    return True


@pure
def spec_task_display_id(task_relative_file: Path) -> str:
    """Dotted, folder-qualified display id for a feature-file task.

    Mirrors coordinate style (``authentication.signin``) so agent/branch
    slugs stay collision-free across folders that reuse a basename (every
    folder may have an ``invariants.feature``).
    """
    return ".".join((*task_relative_file.parent.parts, task_relative_file.stem))


def discover_spec_tasks(
    scan_root: Path,
    area: str | None,
    tag: str | None,
    unit_kind: SpecUnitKind | None,
) -> list[MapReduceTask]:
    """Scan the corpus and group its units into one task per ``.feature`` file.

    Fail-fasts with SpecCorpusInvalidError on any language violation, and with
    NoSpecUnitsError when no unit survives the (AND-composed) filters. Task ids
    are root-relative feature-file paths in corpus scan order.
    """
    scan = scan_corpus(scan_root)
    if scan.violations:
        formatted_violations = "\n".join(_format_corpus_violation(violation) for violation in scan.violations)
        raise SpecCorpusInvalidError(
            f"The behavioral-spec corpus at {scan_root} has language violations; "
            f"fix them (see `mngr specs validate`) before fanning out:\n{formatted_violations}"
        )

    # Group the selected units by feature file, preserving corpus scan order.
    unit_count_by_relative_file: dict[Path, int] = {}
    for unit in scan.units:
        if not _spec_unit_passes_filters(unit, scan_root, area, tag, unit_kind):
            continue
        relative_file = unit.file.relative_to(scan_root)
        unit_count_by_relative_file[relative_file] = unit_count_by_relative_file.get(relative_file, 0) + 1

    if not unit_count_by_relative_file:
        raise NoSpecUnitsError(
            f"No spec units selected from the corpus at {scan_root} "
            f"(area={area!r}, tag={tag!r}, unit kind={unit_kind!r})."
        )

    return [
        MapReduceTask(id=relative_file.as_posix(), display_id=spec_task_display_id(relative_file))
        for relative_file in unit_count_by_relative_file
    ]
