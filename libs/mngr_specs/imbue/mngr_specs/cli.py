"""``mngr specs {validate,list,matrix}`` -- the CLI over a behavioral-spec corpus.

A corpus is any ``<project>/specs/`` directory (``apps/minds/specs`` is this
repo's first corpus); its language is defined by the behavioral-specs skill.
``imbue.mngr_specs.corpus`` / ``.witnesses`` are the scanning/validation engine,
and this module is only the click wiring around it.

Every subcommand takes a required ``--root``: the corpus location. Record
``file`` fields are the paths formed from that root as given, so running
``uv run mngr specs --root <project>/specs ...`` from the repo root yields
repo-relative paths.
"""

import json
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click

from imbue.imbue_common.pure import pure
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_stderr_line
from imbue.mngr_specs.corpus import scan_corpus
from imbue.mngr_specs.corpus import spec_unit_kind_record_value
from imbue.mngr_specs.corpus import spec_unit_matches_area
from imbue.mngr_specs.corpus import spec_unit_matches_name_substring
from imbue.mngr_specs.corpus import spec_unit_matches_step_substring
from imbue.mngr_specs.corpus import spec_unit_matches_tag
from imbue.mngr_specs.corpus import spec_unit_to_record
from imbue.mngr_specs.data_types import CorpusScan
from imbue.mngr_specs.data_types import SpecUnit
from imbue.mngr_specs.data_types import SpecUnitKind
from imbue.mngr_specs.data_types import SpecViolation
from imbue.mngr_specs.data_types import WitnessLink
from imbue.mngr_specs.errors import SpecCorpusRootNotFoundError
from imbue.mngr_specs.errors import SpecDanglingWitnessError
from imbue.mngr_specs.errors import SpecListingIncompleteError
from imbue.mngr_specs.errors import SpecTestsRootNotFoundError
from imbue.mngr_specs.errors import SpecValidationFailedError
from imbue.mngr_specs.witnesses import find_broken_witness_links
from imbue.mngr_specs.witnesses import group_witness_links_by_coordinate
from imbue.mngr_specs.witnesses import harvest_witness_links
from imbue.mngr_specs.witnesses import render_broken_witness_link_diagnostic
from imbue.mngr_specs.witnesses import render_matrix_record


def _root_option(command: Callable[..., None]) -> Callable[..., None]:
    return click.option(
        "--root",
        "corpus_root",
        type=click.Path(file_okay=False, path_type=Path),
        required=True,
        help=(
            "Corpus root directory, conventionally <project>/specs (e.g. apps/minds/specs). "
            "Record file paths are formed from the root as given, so run from the repo root "
            "for repo-relative paths."
        ),
    )(command)


def _require_corpus_root(corpus_root: Path) -> Path:
    if not corpus_root.is_dir():
        raise SpecCorpusRootNotFoundError(
            f"Spec corpus root '{corpus_root}' is not a directory. "
            "Pass --root pointing at a corpus directory (conventionally <project>/specs, e.g. apps/minds/specs)."
        )
    return corpus_root


@pure
def _format_violation(violation: SpecViolation) -> str:
    if violation.line is None:
        return f"{violation.file}: {violation.message}"
    return f"{violation.file}:{violation.line}: {violation.message}"


@click.group(name="specs")
def specs() -> None:
    """Inspect and validate a behavioral-spec corpus.

    A corpus is any `<project>/specs/` directory, named per invocation with
    `--root` (e.g. `--root apps/minds/specs`, this repo's first corpus). The
    corpus language (folders, tags, coordinates, invariants, sidecars) is
    defined by the behavioral-specs skill; `validate` enforces it, `list` emits
    one JSONL record per authored unit (Scenario, Scenario Outline, or Rule),
    optionally filtered by kind, area, tag, name, or step, and `matrix` joins
    the corpus against the `witnesses` test markers to report per-unit coverage.

    Run from the repo root: `uv run mngr specs --root <project>/specs ...`.
    """


# CLI/record spelling of each unit kind, e.g. 'scenario-outline'.
_UNIT_KIND_BY_CLI_VALUE: Final[dict[str, SpecUnitKind]] = {
    spec_unit_kind_record_value(kind): kind for kind in SpecUnitKind
}


def _emit_unit_records(
    units_to_emit: tuple[SpecUnit, ...],
    # The whole corpus, so each record's invariants list can name binding Rules that the emitted subset may exclude.
    all_units: tuple[SpecUnit, ...],
    corpus_root: Path,
) -> None:
    for unit in units_to_emit:
        write_human_line(json.dumps(spec_unit_to_record(unit, all_units, corpus_root), ensure_ascii=False))


@pure
def _omitting_violations(scan: CorpusScan) -> tuple[SpecViolation, ...]:
    return tuple(violation for violation in scan.violations if violation.is_unit_omitted)


def _raise_if_units_were_omitted(omitting_violations: Sequence[SpecViolation]) -> None:
    if omitting_violations:
        raise SpecListingIncompleteError(
            f"the listing is incomplete: {len(omitting_violations)} problem(s) prevented units from being "
            "represented; run `mngr specs validate` for the full picture"
        )


def _fail_if_units_were_omitted(scan: CorpusScan) -> None:
    """Surface unit-omitting problems on stderr and exit nonzero: the emitted listing is incomplete."""
    omitting_violations = _omitting_violations(scan)
    for violation in omitting_violations:
        write_stderr_line(_format_violation(violation))
    _raise_if_units_were_omitted(omitting_violations)


@specs.command(name="validate")
@_root_option
def specs_validate(corpus_root: Path) -> None:
    """Parse every spec file and enforce the behavioral-spec language rules.

    Prints one line per violation (file:line: message, with the line omitted
    where none applies) and exits nonzero if there are any; otherwise prints a
    one-line summary. Checks: gherkin-official parseability; English keywords
    only (no '# language:' headers, no en-dialect synonym spellings outside the
    language's construct list); kebab-case folder names, file basenames, and
    tags; at least one tag on every unit (the first is its identity); unique
    coordinate claims (unit identities plus every Feature/Examples tag);
    reserved 'overview'/'invariants' filenames; and no dangling .md sidecars
    or foreign files.
    """
    scan = scan_corpus(_require_corpus_root(corpus_root))
    for violation in scan.violations:
        write_human_line(_format_violation(violation))
    if scan.violations:
        raise SpecValidationFailedError(f"{len(scan.violations)} violation(s) found under {corpus_root}")
    write_human_line(
        f"OK: {len(scan.units)} units across {scan.feature_file_count} feature file(s) under {corpus_root}"
    )


@pure
def _unit_passes_list_filters(
    unit: SpecUnit,
    corpus_root: Path,
    unit_kind: SpecUnitKind | None,
    area_filter: str | None,
    tag_filter: str | None,
    name_filter: str | None,
    step_filter: str | None,
) -> bool:
    if unit_kind is not None and unit.kind != unit_kind:
        return False
    if area_filter is not None and not spec_unit_matches_area(unit, area_filter, corpus_root):
        return False
    if tag_filter is not None and not spec_unit_matches_tag(unit, tag_filter):
        return False
    if name_filter is not None and not spec_unit_matches_name_substring(unit, name_filter):
        return False
    if step_filter is not None and not spec_unit_matches_step_substring(unit, step_filter):
        return False
    return True


@specs.command(name="list")
@_root_option
@click.option(
    "--unit",
    "unit_kind_value",
    type=click.Choice(sorted(_UNIT_KIND_BY_CLI_VALUE)),
    default=None,
    help="Only emit units of this kind.",
)
@click.option(
    "--area",
    "area_filter",
    default=None,
    help=(
        "Keep units in this folder subtree, named as a dot-joined folder path from the corpus root "
        "(e.g. 'authentication' or 'networking.tunnels'). Matched whole folder segment by segment, so "
        "'auth' does not match the folder 'authentication', and (unlike --tag) it never matches on a unit's "
        "identity tag."
    ),
)
@click.option(
    "--tag",
    "tag_filter",
    default=None,
    help=(
        "Keep units with this exact raw tag (identity or auxiliary; a leading '@' is tolerated) "
        "or this exact coordinate. Auxiliary tags may be shared, so several units can match."
    ),
)
@click.option(
    "--name",
    "name_filter",
    default=None,
    help="Keep units whose name contains this substring (case-insensitive).",
)
@click.option(
    "--step",
    "step_filter",
    default=None,
    help="Keep units where any step text contains this substring (case-insensitive).",
)
def specs_list(
    corpus_root: Path,
    unit_kind_value: str | None,
    area_filter: str | None,
    tag_filter: str | None,
    name_filter: str | None,
    step_filter: str | None,
) -> None:
    """Emit the corpus as JSONL: one record per authored unit on stdout.

    Record fields, in order: coordinate, kind (scenario | scenario-outline |
    rule), name, file (as rooted at --root; repo-relative when run from the repo
    root), line, tags (in authored order, without the '@' sigil; the first is
    the unit's identity), steps (objects with keyword and text; empty for a
    Rule, Background steps not folded in), parent (the enclosing Rule's
    coordinate, or null), and invariants (coordinates of every Rule that binds
    this unit -- Rules in the same file, plus invariants.feature Rules at or
    above the unit's folder -- in corpus order). Units appear in file order,
    then document order.

    The --unit/--area/--tag/--name/--step filters are selection-only and
    AND-composed: a unit is emitted only when it passes every filter given, and
    with no filters every unit is emitted (no match prints nothing, exit 0).
    --area keeps a whole folder subtree; --tag keeps units by exact raw tag or
    exact coordinate (auxiliary tags may be shared across units). A record
    still lists its full invariants even when a binding Rule is filtered out
    of the emitted set. Stdout carries nothing but JSONL; diagnostics go to
    stderr.
    """
    scan = scan_corpus(_require_corpus_root(corpus_root))
    unit_kind = None if unit_kind_value is None else _UNIT_KIND_BY_CLI_VALUE[unit_kind_value]
    matching_units = tuple(
        unit
        for unit in scan.units
        if _unit_passes_list_filters(unit, corpus_root, unit_kind, area_filter, tag_filter, name_filter, step_filter)
    )
    _emit_unit_records(matching_units, scan.units, corpus_root)
    _fail_if_units_were_omitted(scan)


@pure
def _default_test_roots(corpus_root: Path, test_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """The test roots to harvest witnesses from: those passed, or the corpus root's parent by default."""
    if test_roots:
        return test_roots
    return (corpus_root.parent,)


def _require_test_roots(test_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    for test_root in test_roots:
        if not test_root.exists():
            raise SpecTestsRootNotFoundError(
                f"Tests root '{test_root}' does not exist. "
                "Pass --tests, or omit it to default to the corpus root's parent directory."
            )
    return test_roots


def _fail_matrix_if_incomplete_or_broken(scan: CorpusScan, broken_links: Sequence[WitnessLink]) -> None:
    """Surface corpus omissions and broken witness links on stderr, then exit nonzero.

    Coverage gaps are data (exit 0); this only fires for corpus problems that
    omitted units (identical treatment to ``list``) or for markers that name no
    real unit. When both occur, both diagnostic sets print and the omission
    error is raised first.
    """
    omitting_violations = _omitting_violations(scan)
    for violation in omitting_violations:
        write_stderr_line(_format_violation(violation))
    for broken_link in broken_links:
        write_stderr_line(render_broken_witness_link_diagnostic(broken_link))
    _raise_if_units_were_omitted(omitting_violations)
    if broken_links:
        raise SpecDanglingWitnessError(
            f"{len(broken_links)} witnesses marker(s) do not name a real spec unit; "
            "fix the coordinate(s) or the marker usage (see the stderr diagnostics above)"
        )


@specs.command(name="matrix")
@_root_option
@click.option(
    "--tests",
    "test_roots",
    type=click.Path(path_type=Path),
    multiple=True,
    default=(),
    help=(
        "Test root to collect `witnesses` markers from; repeatable. Passed to an inner "
        "pytest --collect-only run, so paths resolve from the current directory. When omitted, "
        "defaults to the corpus root's parent directory (a corpus at <project>/specs is witnessed "
        "by <project>'s tests), so run from the repo root (or pass --tests)."
    ),
)
def specs_matrix(corpus_root: Path, test_roots: tuple[Path, ...]) -> None:
    """Join the corpus against the `witnesses` test markers and emit per-unit coverage as JSONL.

    Runs an inner `pytest --collect-only` over the --tests roots (so it needs
    the dev environment), harvesting every `witnesses(coordinate, partial=...)`
    marker, then emits one JSONL record per corpus unit on stdout, in corpus
    scan order. When --tests is omitted it defaults to the corpus root's parent
    directory (a corpus at <project>/specs is witnessed by <project>'s tests).
    Record fields, in order: coordinate, kind (scenario | scenario-outline |
    rule), name, file (as rooted at --root; repo-relative when run from the repo
    root), line, coverage (full | partial | none), and witnesses (objects with
    the test's pytest node id and its partial note, in collection order). A
    record's coverage is "full" when at least one witnessing test covers the
    unit fully (no partial note), "partial" when witnesses exist but every one
    is partial, and "none" when no test witnesses it.

    Coverage gaps are data, not errors: an all-"none" corpus still exits 0.
    Broken witness links are errors: a marker whose coordinate matches no unit
    (dangling), or invalid marker usage (no positional coordinate, or a
    non-string one), is reported as one `<node id>: ...` line on stderr after
    all stdout records, then exits nonzero. Corpus problems that omit units are
    treated exactly as in `list`. Stdout carries nothing but JSONL.
    """
    scan = scan_corpus(_require_corpus_root(corpus_root))
    resolved_test_roots = _require_test_roots(_default_test_roots(corpus_root, test_roots))
    links = harvest_witness_links(resolved_test_roots)
    links_by_coordinate = group_witness_links_by_coordinate(links)
    for unit in scan.units:
        record = render_matrix_record(unit, links_by_coordinate.get(unit.coordinate, []))
        write_human_line(json.dumps(record, ensure_ascii=False))
    unit_coordinates = frozenset(unit.coordinate for unit in scan.units)
    broken_links = find_broken_witness_links(links, unit_coordinates)
    _fail_matrix_if_incomplete_or_broken(scan, broken_links)
