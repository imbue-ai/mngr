"""The test-mapreduce recipe: a :class:`MapReduceRecipe` for fanning out pytest tests.

The recipe encapsulates everything test-specific: discovery (pytest collect),
the mapper prompt (run the test, propose fixes), the reducer prompt (cherry-
pick the per-mapper fix bundles into a linear stack), and the HTML report
that interprets each mapper's outcome JSON. The framework
(``imbue.mngr_mapreduce``) handles agent launching, polling, output
extraction, and CLI plumbing.
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import field_validator

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.data_types import ReducerInfo
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.report import generate_html_report
from imbue.mngr_tmr.report_upload import maybe_upload_report

BRANCH_BUNDLE_NAME = "branch.bundle"

_DEFAULT_RECIPE_NAME = "tmr"

# The recipe name becomes a segment of git branch names (``<name>/<run>/<slug>``)
# and agent/host names (``<name>-<run>-<slug>``), so it must be a conservative
# slug: an alphanumeric start followed by alphanumerics, dashes, or underscores.
_RECIPE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class CollectTestsError(MngrError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


class InvalidRecipeNameError(MngrError, ValueError):
    """Raised when a recipe/variant name is not a safe branch/agent name segment.

    Inherits from ``ValueError`` so pydantic wraps it into a ``ValidationError``
    when raised from the ``name`` field validator.
    """

    ...


def validate_recipe_name(value: str) -> str:
    """Validate a recipe/variant name for use as a branch/agent/host name segment.

    Shared by every recipe's ``name`` field validator. Raises
    InvalidRecipeNameError (a ValueError) so pydantic wraps it.
    """
    if not _RECIPE_NAME_PATTERN.match(value):
        raise InvalidRecipeNameError(
            f"Invalid recipe name {value!r}: must start with an alphanumeric and contain only "
            "alphanumerics, dashes, or underscores (it becomes a branch/agent/host name segment)."
        )
    return value


def collect_tests(
    pytest_args: tuple[str, ...],
    source_dir: Path,
    cg: ConcurrencyGroup,
) -> list[str]:
    """Run pytest --collect-only -q and return the list of test node IDs."""
    cmd = ["python", "-m", "pytest", "--collect-only", "-q", *pytest_args]
    logger.info("Collecting tests: {}", " ".join(cmd))
    result = cg.run_process_to_completion(cmd, cwd=source_dir, timeout=60.0, is_checked_after=False)
    if result.returncode != 0:
        raise CollectTestsError(f"pytest --collect-only failed (exit code {result.returncode}):\n{result.stderr}")

    test_ids: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped and "::" in stripped and not stripped.startswith("="):
            test_ids.append(stripped)

    if not test_ids:
        raise CollectTestsError("pytest --collect-only returned no tests")

    logger.info("Collected {} test(s)", len(test_ids))
    return test_ids


def apply_branch_bundle(
    source_dir: Path,
    bundle_path: Path,
    branch_name: str,
    agent_name: str,
    cg: ConcurrencyGroup,
) -> bool:
    """Fetch a branch from a bundle into the local source_dir repo.

    The bundle was created with ``git bundle create ... <base>..<branch>``,
    so it carries the ref under its branch name; the fetch refspec maps
    that ref onto the same local branch name. Idempotent for repeated
    invocations. Returns True on success.
    """
    result = cg.run_process_to_completion(
        ["git", "fetch", "--no-tags", str(bundle_path), f"+{branch_name}:{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to apply branch bundle for agent '{}' (branch {}): {}",
            agent_name,
            branch_name,
            result.stderr.strip(),
        )
        return False
    logger.info("Applied branch bundle for agent '{}' onto branch '{}'", agent_name, branch_name)
    return True


def has_local_branch(source_dir: Path, branch_name: str, cg: ConcurrencyGroup) -> bool:
    """Check whether a git branch exists in the local source_dir repo."""
    result = cg.run_process_to_completion(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    return result.returncode == 0


def reducer_branch_applied(
    ctx: MapReduceContext,
    reducer: AgentMetadata | None,
) -> bool:
    """Did the reducer succeed and land a usable local branch?

    Read from git rather than recipe-side state so the recipe stays stateless
    (and ``render_report`` is safe to call repeatedly during polling).
    """
    if reducer is None or reducer.branch_name is None or reducer.error_summary is not None:
        return False
    return has_local_branch(ctx.source_dir, reducer.branch_name, ctx.cg)


class TestMapReduceRecipe(MapReduceRecipe, FrozenModel):
    """Run and fix pytest tests in parallel using one agent per test.

    Each mapper agent runs one test and (if it fails) attempts to fix
    either the test or the implementation. Their per-mapper branches
    are uploaded back as ``branch.bundle`` files inside the outputs
    archive; this recipe applies each bundle to the local source repo
    in ``on_mapper_finalized``.

    The reducer agent then cherry-picks the qualifying mapper branches
    into a single linear stack on its own ``tmr/<run>/reducer`` branch;
    that branch's bundle is similarly fetched into the local repo in
    ``on_reducer_finalized``.
    """

    # The "Test" prefix is historical (test map-reduce), not a pytest test
    # class; tell pytest not to try to collect it.
    __test__ = False

    name: str = Field(
        default=_DEFAULT_RECIPE_NAME,
        description="Variant name; prefixes this run's agent/branch/host names so distinct suites "
        "(e.g. tmr-mngr vs tmr-minds) stay separable and reviewable on their own.",
    )
    pytest_args: tuple[str, ...] = Field(default=(), description="Positional pytest paths/patterns")
    testing_flags: tuple[str, ...] = Field(
        default=(), description="Flags shared between pytest discovery and individual mapper runs"
    )
    mapper_prompt_path: Path | None = Field(
        default=None,
        description="Optional override template for the mapper prompt (falls back to the packaged mapper.j2)",
    )
    reducer_prompt_path: Path | None = Field(
        default=None,
        description="Optional override template for the reducer prompt (falls back to the packaged reducer.j2)",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_recipe_name(value)

    def discover(self, ctx: MapReduceContext) -> list[MapReduceTask]:
        raw_ids = collect_tests(
            pytest_args=self.pytest_args + self.testing_flags,
            source_dir=ctx.source_dir,
            cg=ctx.cg,
        )
        return [
            MapReduceTask(
                id=tid,
                # The last "::"-segment of a pytest node id is the test name;
                # using it as display_id keeps agent/branch slugs short.
                display_id=tid.split("::")[-1] if "::" in tid else None,
            )
            for tid in raw_ids
        ]

    def build_mapper_prompt(self, ctx: MapReduceContext, task: MapReduceTask) -> str:
        # The e2e run-name flag (which lands per-try artifacts under
        # .test_output/e2e/tmr_<run>_try_N/) is registered only by the mngr e2e
        # conftest, so it is valid only for e2e tests. Other release tests (e.g.
        # install/docker/cli, or the per-provider packages) would error on an
        # unrecognized argument, so only e2e tests get it.
        is_e2e = "/e2e/" in task.id
        e2e_run_name = f"{self.name}_{ctx.run_name}" if is_e2e else None
        return build_test_agent_prompt(
            task.id, self.testing_flags, e2e_run_name, template_path=self.mapper_prompt_path
        )

    def build_reducer_prompt(self, ctx: MapReduceContext) -> str:
        return build_integrator_prompt(template_path=self.reducer_prompt_path)

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
        if has_local_branch(ctx.source_dir, info.branch_name, ctx.cg):
            emit_reducer_branch(info.branch_name, ctx.output_opts)

    def render_report(
        self,
        ctx: MapReduceContext,
        agents: Sequence[AgentMetadata],
        reducer: AgentMetadata | None,
    ) -> Path | None:
        # Only surface a "Push integrated branch" hint when the reducer's
        # bundle actually landed locally; otherwise the command would
        # reference a nonexistent branch.
        applied = reducer_branch_applied(ctx, reducer)
        run_commands = _build_run_commands(
            ctx.run_name,
            recipe_name=self.name,
            integrated_branch=reducer.branch_name if applied and reducer is not None else None,
        )
        report_path = generate_html_report(
            agents=agents,
            output_dir=ctx.output_dir,
            integrator_metadata=reducer,
            run_commands=run_commands,
        )
        # Mirror to S3 (no-op without AWS creds) on every regeneration;
        # symmetric with the local file write.
        emit_report_url(maybe_upload_report(report_path, ctx.run_name), ctx.output_opts)
        return report_path


def _build_run_commands(
    run_name: str, recipe_name: str = _DEFAULT_RECIPE_NAME, integrated_branch: str | None = None
) -> list[tuple[str, str]]:
    """Build a list of (label, command) pairs for the run.

    ``recipe_name`` is threaded into the reintegrate hint (as ``--name``) for
    non-default variants so it resolves the run's output dir consistently.
    """
    name_flag = "" if recipe_name == _DEFAULT_RECIPE_NAME else f"--name {recipe_name} "
    commands = [
        ("List agents from this run", f"mngr ls --include 'labels.mapreduce_run_name == \"{run_name}\"'"),
        ("Reintegrate", f"mngr tmr {name_flag}--reintegrate --run-name {run_name}"),
    ]
    if integrated_branch is not None:
        commands.append(("Push integrated branch", f"git push origin {integrated_branch}"))
    return commands


def emit_reducer_branch(branch_name: str, output_opts: OutputOptions) -> None:
    """Emit the integrator branch name as a structured event when applicable."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("integrator_branch", {"branch_name": branch_name}, output_opts.output_format)
        case OutputFormat.HUMAN:
            pass
        case _ as unreachable:
            assert_never(unreachable)


def emit_report_url(url: str | None, output_opts: OutputOptions) -> None:
    """Emit the public URL of the report mirror, if upload occurred."""
    if url is None:
        return
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("report_url", {"url": url}, output_opts.output_format)
        case OutputFormat.HUMAN:
            write_human_line("Report URL: {}", url)
        case _ as unreachable:
            assert_never(unreachable)
