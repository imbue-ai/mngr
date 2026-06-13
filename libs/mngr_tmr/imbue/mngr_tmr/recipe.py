"""The test-mapreduce recipe: a :class:`MapReduceRecipe` for fanning out pytest tests.

The recipe encapsulates everything test-specific: discovery (pytest collect),
the mapper prompt (run the test, propose fixes), the reducer prompt (cherry-
pick the per-mapper fix bundles into a linear stack), and the HTML report
that interprets each mapper's outcome JSON. The framework
(``imbue.mngr_mapreduce``) handles agent launching, polling, output
extraction, and CLI plumbing.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar
from typing import assert_never

from loguru import logger
from pydantic import Field

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

_BRANCH_BUNDLE_NAME = "branch.bundle"


class CollectTestsError(MngrError, RuntimeError):
    """Raised when pytest test collection fails."""

    ...


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


def _apply_branch_bundle(
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


def _has_local_branch(source_dir: Path, branch_name: str, cg: ConcurrencyGroup) -> bool:
    """Check whether a git branch exists in the local source_dir repo."""
    result = cg.run_process_to_completion(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    return result.returncode == 0


def _reducer_branch_applied(
    ctx: MapReduceContext,
    reducer: AgentMetadata | None,
) -> bool:
    """Did the reducer succeed and land a usable local branch?

    Read from git rather than recipe-side state so the recipe stays stateless
    (and ``render_report`` is safe to call repeatedly during polling).
    """
    if reducer is None or reducer.branch_name is None or reducer.error_summary is not None:
        return False
    return _has_local_branch(ctx.source_dir, reducer.branch_name, ctx.cg)


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

    name: ClassVar[str] = "tmr"

    pytest_args: tuple[str, ...] = Field(default=(), description="Positional pytest paths/patterns")
    testing_flags: tuple[str, ...] = Field(
        default=(), description="Flags shared between pytest discovery and individual mapper runs"
    )

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
        # Append the e2e run-name flag so each agent's per-try artifacts land
        # under .test_output/e2e/tmr_<run>_try_N/ rather than colliding with
        # ad-hoc local pytest runs.
        flags = self.testing_flags + ("--mngr-e2e-run-name", f"{self.name}_{ctx.run_name}")
        return build_test_agent_prompt(task.id, flags)

    def build_reducer_prompt(self, ctx: MapReduceContext) -> str:
        return build_integrator_prompt()

    def on_mapper_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: MapperInfo) -> None:
        bundle = agent_dir / _BRANCH_BUNDLE_NAME
        if bundle.is_file():
            _apply_branch_bundle(ctx.source_dir, bundle, info.branch_name, str(info.agent_name), ctx.cg)

    def on_reducer_finalized(self, ctx: MapReduceContext, agent_dir: Path, info: ReducerInfo) -> None:
        bundle = agent_dir / _BRANCH_BUNDLE_NAME
        if not bundle.is_file():
            logger.warning("Reducer agent '{}' did not produce a branch bundle", info.agent_name)
            return
        if not _apply_branch_bundle(ctx.source_dir, bundle, info.branch_name, str(info.agent_name), ctx.cg):
            return
        if _has_local_branch(ctx.source_dir, info.branch_name, ctx.cg):
            _emit_reducer_branch(info.branch_name, ctx.output_opts)

    def render_report(
        self,
        ctx: MapReduceContext,
        agents: Sequence[AgentMetadata],
        reducer: AgentMetadata | None,
    ) -> Path | None:
        # Only surface a "Push integrated branch" hint when the reducer's
        # bundle actually landed locally; otherwise the command would
        # reference a nonexistent branch.
        applied = _reducer_branch_applied(ctx, reducer)
        run_commands = _build_run_commands(
            ctx.run_name,
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
        _emit_report_url(maybe_upload_report(report_path, ctx.run_name), ctx.output_opts)
        return report_path


def _build_run_commands(run_name: str, integrated_branch: str | None = None) -> list[tuple[str, str]]:
    """Build a list of (label, command) pairs for the run."""
    commands = [
        ("List agents from this run", f"mngr ls --include 'labels.mapreduce_run_name == \"{run_name}\"'"),
        ("Reintegrate", f"mngr tmr --reintegrate --run-name {run_name}"),
    ]
    if integrated_branch is not None:
        commands.append(("Push integrated branch", f"git push origin {integrated_branch}"))
    return commands


def _emit_reducer_branch(branch_name: str, output_opts: OutputOptions) -> None:
    """Emit the integrator branch name as a structured event when applicable."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("integrator_branch", {"branch_name": branch_name}, output_opts.output_format)
        case OutputFormat.HUMAN:
            pass
        case _ as unreachable:
            assert_never(unreachable)


def _emit_report_url(url: str | None, output_opts: OutputOptions) -> None:
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
