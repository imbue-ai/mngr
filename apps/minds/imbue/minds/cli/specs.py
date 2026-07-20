"""``minds specs {validate,list}`` -- the CLI over the behavioral-spec corpus.

The corpus (``apps/minds/specs/`` in this repo) and its language are defined by
the minds-behavioral-specs skill; ``imbue.minds.core.behavioral_specs`` is the
scanning/validation engine, and this module is only the click wiring around it.

Every subcommand takes ``--root``: the corpus location, defaulting to the real
corpus relative to the current directory (so the documented invocation is
``uv run minds specs ...`` from the repo root). Record ``file`` fields are the
paths formed from that root as given, which makes them repo-relative for the
default invocation.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import click

from imbue.imbue_common.pure import pure
from imbue.minds.core.behavioral_specs.corpus import scan_corpus
from imbue.minds.core.behavioral_specs.corpus import spec_unit_kind_record_value
from imbue.minds.core.behavioral_specs.corpus import spec_unit_matches_area
from imbue.minds.core.behavioral_specs.corpus import spec_unit_matches_name_substring
from imbue.minds.core.behavioral_specs.corpus import spec_unit_matches_step_substring
from imbue.minds.core.behavioral_specs.corpus import spec_unit_matches_tag
from imbue.minds.core.behavioral_specs.corpus import spec_unit_to_record
from imbue.minds.core.behavioral_specs.data_types import CorpusScan
from imbue.minds.core.behavioral_specs.data_types import SpecUnit
from imbue.minds.core.behavioral_specs.data_types import SpecUnitKind
from imbue.minds.core.behavioral_specs.data_types import SpecViolation
from imbue.minds.errors import SpecCorpusRootNotFoundError
from imbue.minds.errors import SpecListingIncompleteError
from imbue.minds.errors import SpecValidationFailedError
from imbue.minds.utils.output import write_stdout_line
from imbue.mngr.cli.output_helpers import write_stderr_line

# The real corpus, relative to the repo root (the documented working directory
# for ``uv run minds specs ...``).
DEFAULT_CORPUS_ROOT: Final[Path] = Path("apps/minds/specs")


def _root_option(command: Callable[..., None]) -> Callable[..., None]:
    return click.option(
        "--root",
        "corpus_root",
        type=click.Path(file_okay=False, path_type=Path),
        default=DEFAULT_CORPUS_ROOT,
        show_default=True,
        help=(
            "Corpus root directory. The default is the real corpus relative to the "
            "current directory, so run from the repo root (or pass --root)."
        ),
    )(command)


def _require_corpus_root(corpus_root: Path) -> Path:
    if not corpus_root.is_dir():
        raise SpecCorpusRootNotFoundError(
            f"Spec corpus root '{corpus_root}' is not a directory. "
            f"Run from the repo root (where the default '{DEFAULT_CORPUS_ROOT}' exists) or pass --root."
        )
    return corpus_root


@pure
def _format_violation(violation: SpecViolation) -> str:
    if violation.line is None:
        return f"{violation.file}: {violation.message}"
    return f"{violation.file}:{violation.line}: {violation.message}"


@click.group(name="specs")
def specs() -> None:
    """Inspect and validate the behavioral-spec corpus (apps/minds/specs).

    The corpus language (folders, tags, coordinates, invariants, sidecars) is
    defined by the minds-behavioral-specs skill; `validate` enforces it, and
    `list` emits one JSONL record per authored unit (Scenario, Scenario
    Outline, or Rule), optionally filtered by kind, area, tag, name, or step.
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
    unit_kind: SpecUnitKind | None,
) -> None:
    for unit in units_to_emit:
        if unit_kind is not None and unit.kind != unit_kind:
            continue
        write_stdout_line(json.dumps(spec_unit_to_record(unit, all_units, corpus_root), ensure_ascii=False))


def _fail_if_units_were_omitted(scan: CorpusScan) -> None:
    """Surface unit-omitting problems on stderr and exit nonzero: the emitted listing is incomplete."""
    omitting_violations = tuple(violation for violation in scan.violations if violation.is_unit_omitted)
    if not omitting_violations:
        return
    for violation in omitting_violations:
        write_stderr_line(_format_violation(violation))
    raise SpecListingIncompleteError(
        f"the listing is incomplete: {len(omitting_violations)} problem(s) prevented units from being "
        "represented; run `minds specs validate` for the full picture"
    )


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
        write_stdout_line(_format_violation(violation))
    if scan.violations:
        raise SpecValidationFailedError(f"{len(scan.violations)} violation(s) found under {corpus_root}")
    write_stdout_line(
        f"OK: {len(scan.units)} units across {scan.feature_file_count} feature file(s) under {corpus_root}"
    )


@pure
def _unit_passes_list_filters(
    unit: SpecUnit,
    corpus_root: Path,
    area_filter: str | None,
    tag_filter: str | None,
    name_filter: str | None,
    step_filter: str | None,
) -> bool:
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
        "Keep the single unit with this exact raw tag (identity or auxiliary; a leading '@' is tolerated) "
        "or this exact coordinate."
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
    rule), name, file (as rooted at --root; repo-relative for the default
    invocation from the repo root), line, tags (in authored order, without the
    '@' sigil; the first is the unit's identity), steps (objects with keyword
    and text; empty for a Rule, Background steps not folded in), parent (the
    enclosing Rule's coordinate, or null), and invariants (coordinates of every
    Rule that binds this unit -- Rules in the same file, plus invariants.feature
    Rules at or above the unit's folder -- in corpus order). Units appear in
    file order, then document order.

    The --unit/--area/--tag/--name/--step filters are selection-only and
    AND-composed: a unit is emitted only when it passes every filter given, and
    with no filters every unit is emitted (no match prints nothing, exit 0).
    --area keeps a whole folder subtree; --tag keeps a single unit by exact raw
    tag or coordinate. A record still lists its full invariants even when a
    binding Rule is filtered out of the emitted set. Stdout carries nothing but
    JSONL; diagnostics go to stderr.
    """
    scan = scan_corpus(_require_corpus_root(corpus_root))
    unit_kind = None if unit_kind_value is None else _UNIT_KIND_BY_CLI_VALUE[unit_kind_value]
    matching_units = tuple(
        unit
        for unit in scan.units
        if _unit_passes_list_filters(unit, corpus_root, area_filter, tag_filter, name_filter, step_filter)
    )
    _emit_unit_records(matching_units, scan.units, corpus_root, unit_kind)
    _fail_if_units_were_omitted(scan)
