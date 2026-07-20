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

The corpus is read-only to the whole pipeline. The prompts state it, and
``on_reducer_finalized`` enforces it mechanically at the single egress: a
reducer branch whose diff touches the corpus root is never emitted.
"""

from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import field_validator

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.data_types import ReducerInfo
from imbue.mngr_specs.corpus import CorpusScan
from imbue.mngr_specs.corpus import binding_invariant_coordinates
from imbue.mngr_specs.corpus import scan_corpus
from imbue.mngr_specs.corpus import spec_unit_kind_record_value
from imbue.mngr_specs.corpus import spec_unit_matches_area
from imbue.mngr_specs.corpus import spec_unit_matches_tag
from imbue.mngr_specs.data_types import SpecUnit
from imbue.mngr_specs.data_types import SpecUnitKind
from imbue.mngr_specs.data_types import SpecViolation
from imbue.mngr_tmr.recipe import BRANCH_BUNDLE_NAME
from imbue.mngr_tmr.recipe import apply_branch_bundle
from imbue.mngr_tmr.recipe import emit_reducer_branch
from imbue.mngr_tmr.recipe import emit_report_url
from imbue.mngr_tmr.recipe import has_local_branch
from imbue.mngr_tmr.recipe import reducer_branch_applied
from imbue.mngr_tmr.recipe import validate_recipe_name
from imbue.mngr_tmr.report_upload import maybe_upload_report
from imbue.mngr_tmr.spec_prompts import SpecUnitPromptView
from imbue.mngr_tmr.spec_prompts import build_spec_mapper_prompt
from imbue.mngr_tmr.spec_prompts import build_spec_reducer_prompt
from imbue.mngr_tmr.spec_report import generate_spec_html_report

_DEFAULT_SPEC_RECIPE_NAME = "tmr-specs"


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


class MissingTestRootsError(MngrError, ValueError):
    """Raised when a spec recipe is constructed without any test roots.

    The CLI resolves the default (the corpus root's parent) before the recipe
    is built, so the recipe itself always holds the effective roots.
    """

    ...


class CorpusGateError(MngrError, RuntimeError):
    """Raised when the corpus egress gate cannot determine a branch's diff against its base.

    Callers treat this as fail-closed: an unverifiable reducer branch is
    never emitted.
    """

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


def _scan_valid_corpus(scan_root: Path) -> CorpusScan:
    """Scan the corpus, fail-fasting on any language violation."""
    scan = scan_corpus(scan_root)
    if scan.violations:
        formatted_violations = "\n".join(_format_corpus_violation(violation) for violation in scan.violations)
        raise SpecCorpusInvalidError(
            f"The behavioral-spec corpus at {scan_root} has language violations; "
            f"fix them (see `mngr specs validate`) before fanning out:\n{formatted_violations}"
        )
    return scan


@pure
def _selected_units_by_relative_file(
    scan: CorpusScan,
    scan_root: Path,
    area: str | None,
    tag: str | None,
    unit_kind: SpecUnitKind | None,
) -> dict[Path, list[SpecUnit]]:
    """Group the filter-selected units by root-relative feature file, in corpus scan order."""
    units_by_relative_file: dict[Path, list[SpecUnit]] = {}
    for unit in scan.units:
        if not _spec_unit_passes_filters(unit, scan_root, area, tag, unit_kind):
            continue
        units_by_relative_file.setdefault(unit.file.relative_to(scan_root), []).append(unit)
    return units_by_relative_file


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
    scan = _scan_valid_corpus(scan_root)
    units_by_relative_file = _selected_units_by_relative_file(scan, scan_root, area, tag, unit_kind)
    if not units_by_relative_file:
        raise NoSpecUnitsError(
            f"No spec units selected from the corpus at {scan_root} "
            f"(area={area!r}, tag={tag!r}, unit kind={unit_kind!r})."
        )
    return [
        MapReduceTask(id=relative_file.as_posix(), display_id=spec_task_display_id(relative_file))
        for relative_file in units_by_relative_file
    ]


def build_spec_mapper_prompt_for_task(
    scan_root: Path,
    # The corpus root as the caller named it (repo-relative), used in the prompt's paths/commands.
    corpus_root_display: Path,
    task_id: str,
    area: str | None,
    tag: str | None,
    unit_kind: SpecUnitKind | None,
    test_roots_display: tuple[Path, ...],
    testing_flags: tuple[str, ...],
    template_path: Path | None,
) -> str:
    """Assemble the mapper prompt for one feature-file task from a fresh corpus scan.

    Re-scanning per task keeps the recipe stateless (a corpus parse is
    milliseconds); the same filters used at discovery select the task's units.
    """
    scan = _scan_valid_corpus(scan_root)
    units_by_relative_file = _selected_units_by_relative_file(scan, scan_root, area, tag, unit_kind)
    task_units = units_by_relative_file.get(Path(task_id))
    if not task_units:
        raise NoSpecUnitsError(f"Task {task_id!r} selects no units in the corpus at {scan_root}.")
    unit_views = tuple(
        SpecUnitPromptView(
            coordinate=unit.coordinate,
            kind=spec_unit_kind_record_value(unit.kind),
            name=unit.name,
            line=unit.line,
            parent=unit.parent,
            invariants=binding_invariant_coordinates(unit, scan.units, scan_root),
        )
        for unit in task_units
    )
    return build_spec_mapper_prompt(
        feature_path=(corpus_root_display / task_id).as_posix(),
        units=unit_views,
        corpus_root=corpus_root_display.as_posix(),
        test_roots=tuple(test_root.as_posix() for test_root in test_roots_display),
        testing_flags=testing_flags,
        template_path=template_path,
    )


def corpus_touching_paths(
    source_dir: Path,
    branch_name: str,
    corpus_root: Path,
    cg: ConcurrencyGroup,
) -> tuple[str, ...]:
    """Paths under the corpus root that ``branch_name`` changes relative to its merge base with HEAD.

    Empty means the branch is clean. Raises CorpusGateError when git cannot
    answer (unknown branch, no common history) -- callers fail closed.
    """
    merge_base_result = cg.run_process_to_completion(
        ["git", "merge-base", "HEAD", branch_name], cwd=source_dir, is_checked_after=False
    )
    if merge_base_result.returncode != 0:
        raise CorpusGateError(
            f"Cannot find the merge base of HEAD and {branch_name!r}: {merge_base_result.stderr.strip()}"
        )
    merge_base = merge_base_result.stdout.strip()
    diff_result = cg.run_process_to_completion(
        ["git", "diff", "--name-only", f"{merge_base}..{branch_name}", "--", corpus_root.as_posix()],
        cwd=source_dir,
        is_checked_after=False,
    )
    if diff_result.returncode != 0:
        raise CorpusGateError(
            f"Cannot diff {branch_name!r} against its merge base over {corpus_root}: {diff_result.stderr.strip()}"
        )
    return tuple(line.strip() for line in diff_result.stdout.splitlines() if line.strip())


class SpecMapReduceRecipe(MapReduceRecipe, FrozenModel):
    """Create and update the tests witnessing a behavioral-spec corpus, one agent per spec file.

    Each mapper agent owns one ``.feature`` file's units and converges their
    witnessing tests to the units' scope (creating missing witnesses,
    trimming gold-plating, honoring the witnesses-marker conventions). The
    reducer integrates the per-mapper branches exactly as TMR does, then
    audits the witness links by running ``mngr specs matrix`` over the
    integrated tree.
    """

    name: str = Field(
        default=_DEFAULT_SPEC_RECIPE_NAME,
        description="Variant name; prefixes this run's agent/branch/host names so distinct corpora "
        "(e.g. tmr-specs-minds) stay separable and reviewable on their own.",
    )
    corpus_root: Path = Field(
        description="Behavioral-spec corpus root, repo-relative (conventionally <project>/specs)"
    )
    test_roots: tuple[Path, ...] = Field(
        description="Test roots witnessing the corpus, repo-relative; the CLI defaults this to the corpus "
        "root's parent, so it is always non-empty here"
    )
    area: str | None = Field(default=None, description="Only fan out units in this dot-joined folder subtree")
    tag: str | None = Field(default=None, description="Only fan out units with this exact tag or coordinate")
    unit_kind: SpecUnitKind | None = Field(default=None, description="Only fan out units of this kind")
    testing_flags: tuple[str, ...] = Field(default=(), description="Flags appended to the mappers' pytest invocations")
    mapper_prompt_path: Path | None = Field(
        default=None,
        description="Optional override template for the mapper prompt (falls back to the packaged "
        "spec_mapper.j2; it may {% extends %} the packaged template and fill its blocks)",
    )
    reducer_prompt_path: Path | None = Field(
        default=None,
        description="Optional override template for the reducer prompt (falls back to the packaged spec_reducer.j2)",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_recipe_name(value)

    @field_validator("test_roots")
    @classmethod
    def _validate_test_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if not value:
            raise MissingTestRootsError(
                "A spec recipe needs at least one test root; the CLI defaults it to the corpus root's parent."
            )
        return value

    def discover(self, ctx: MapReduceContext) -> list[MapReduceTask]:
        return discover_spec_tasks(
            scan_root=ctx.source_dir / self.corpus_root,
            area=self.area,
            tag=self.tag,
            unit_kind=self.unit_kind,
        )

    def build_mapper_prompt(self, ctx: MapReduceContext, task: MapReduceTask) -> str:
        return build_spec_mapper_prompt_for_task(
            scan_root=ctx.source_dir / self.corpus_root,
            corpus_root_display=self.corpus_root,
            task_id=task.id,
            area=self.area,
            tag=self.tag,
            unit_kind=self.unit_kind,
            test_roots_display=self.test_roots,
            testing_flags=self.testing_flags,
            template_path=self.mapper_prompt_path,
        )

    def build_reducer_prompt(self, ctx: MapReduceContext) -> str:
        return build_spec_reducer_prompt(
            corpus_root=self.corpus_root.as_posix(),
            test_roots=tuple(test_root.as_posix() for test_root in self.test_roots),
            template_path=self.reducer_prompt_path,
        )

    def on_mapper_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: MapperInfo) -> None:
        bundle = agent_dir / BRANCH_BUNDLE_NAME
        if bundle.is_file():
            apply_branch_bundle(ctx.source_dir, bundle, info.branch_name, str(info.agent_name), ctx.cg)

    def on_reducer_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: ReducerInfo) -> None:
        bundle = agent_dir / BRANCH_BUNDLE_NAME
        if not bundle.is_file():
            logger.warning("Reducer agent '{}' did not produce a branch bundle", info.agent_name)
            return
        if not apply_branch_bundle(ctx.source_dir, bundle, info.branch_name, str(info.agent_name), ctx.cg):
            return
        if not has_local_branch(ctx.source_dir, info.branch_name, ctx.cg):
            return
        # The corpus egress gate: the one unconditional enforcement point of
        # the read-only corpus. A CorpusGateError propagates (the framework
        # logs it), so an unverifiable branch is never emitted either.
        touching = corpus_touching_paths(ctx.source_dir, info.branch_name, self.corpus_root, ctx.cg)
        if touching:
            logger.error(
                "Refusing to emit reducer branch '{}': it touches the read-only spec corpus ({})",
                info.branch_name,
                ", ".join(touching),
            )
            return
        emit_reducer_branch(info.branch_name, ctx.output_opts)

    def render_report(
        self,
        ctx: MapReduceContext,
        agents: Sequence[AgentMetadata],
        reducer: AgentMetadata | None,
    ) -> Path | None:
        applied = reducer_branch_applied(ctx, reducer)
        violation_paths = self._gate_finding_for_report(ctx, reducer) if applied else None
        is_branch_published = applied and violation_paths == ()
        run_commands = _build_spec_run_commands(
            ctx.run_name,
            recipe_name=self.name,
            corpus_root=self.corpus_root,
            integrated_branch=reducer.branch_name if is_branch_published and reducer is not None else None,
        )
        report_path = generate_spec_html_report(
            agents=agents,
            output_dir=ctx.output_dir,
            integrator_metadata=reducer,
            run_commands=run_commands,
            corpus_violation_paths=violation_paths,
        )
        emit_report_url(maybe_upload_report(report_path, ctx.run_name), ctx.output_opts)
        return report_path

    def _gate_finding_for_report(self, ctx: MapReduceContext, reducer: AgentMetadata | None) -> tuple[str, ...] | None:
        """Derive the gate finding at render time (git is the single fact source)."""
        if reducer is None or reducer.branch_name is None:
            return None
        try:
            return corpus_touching_paths(ctx.source_dir, reducer.branch_name, self.corpus_root, ctx.cg)
        except CorpusGateError as exc:
            logger.warning("Corpus gate could not verify branch '{}': {}", reducer.branch_name, exc)
            return None


def _build_spec_run_commands(
    run_name: str, recipe_name: str, corpus_root: Path, integrated_branch: str | None
) -> list[tuple[str, str]]:
    """Build (label, command) pairs for the report header, mirroring the TMR ones.

    Unlike ``mngr tmr``, the reintegrate hint must carry ``--root`` (a
    required option) so the rebuilt recipe points at the same corpus.
    """
    name_flag = "" if recipe_name == _DEFAULT_SPEC_RECIPE_NAME else f"--name {recipe_name} "
    commands = [
        ("List agents from this run", f"mngr ls --include 'labels.mapreduce_run_name == \"{run_name}\"'"),
        (
            "Reintegrate",
            f"mngr tmr-specs --root {corpus_root.as_posix()} {name_flag}--reintegrate --run-name {run_name}",
        ),
    ]
    if integrated_branch is not None:
        commands.append(("Push integrated branch", f"git push origin {integrated_branch}"))
    return commands
