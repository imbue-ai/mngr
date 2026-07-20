"""Branch-bundle retrieval for map-reduce recipes.

Agents package their commits as an incremental git bundle
(``git bundle create branch.bundle <base>..<branch>``) inside their outputs
archive. The helpers here fetch such bundles back into the local source repo
(idempotently), check whether a fetched branch landed, and emit the reducer
branch event. They are shared by every recipe that pulls agent branches back
(the test fan-out recipe, the task-file recipe).
"""

from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import OutputFormat
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import ReducerInfo

BRANCH_BUNDLE_NAME: Final[str] = "branch.bundle"


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


def apply_agent_branch_bundle_if_present(
    source_dir: Path,
    agent_dir: Path,
    branch_name: str,
    agent_name: str,
    cg: ConcurrencyGroup,
) -> bool:
    """Apply an agent's extracted branch bundle when it published one. Returns True when applied."""
    bundle = agent_dir / BRANCH_BUNDLE_NAME
    if not bundle.is_file():
        return False
    return apply_branch_bundle(source_dir, bundle, branch_name, agent_name, cg)


def reducer_branch_applied(
    source_dir: Path,
    reducer: AgentMetadata | None,
    cg: ConcurrencyGroup,
) -> bool:
    """Did the reducer succeed and land a usable local branch?

    Read from git rather than recipe-side state so the recipe stays stateless
    (and ``render_report`` is safe to call repeatedly during polling).
    """
    if reducer is None or reducer.branch_name is None or reducer.error_summary is not None:
        return False
    return has_local_branch(source_dir, reducer.branch_name, cg)


def emit_reducer_branch(branch_name: str, output_opts: OutputOptions) -> None:
    """Emit the integrator branch name as a structured event when applicable."""
    match output_opts.output_format:
        case OutputFormat.JSON | OutputFormat.JSONL:
            emit_event("integrator_branch", {"branch_name": branch_name}, output_opts.output_format)
        case OutputFormat.HUMAN:
            pass
        case _ as unreachable:
            assert_never(unreachable)


def finalize_reducer_branch(ctx: MapReduceContext, agent_dir: Path, info: ReducerInfo) -> None:
    """Apply the reducer's extracted branch bundle and emit the branch event when it lands."""
    bundle = agent_dir / BRANCH_BUNDLE_NAME
    if not bundle.is_file():
        logger.warning("Reducer agent '{}' did not produce a branch bundle", info.agent_name)
        return
    if not apply_branch_bundle(ctx.source_dir, bundle, info.branch_name, str(info.agent_name), ctx.cg):
        return
    if has_local_branch(ctx.source_dir, info.branch_name, ctx.cg):
        emit_reducer_branch(info.branch_name, ctx.output_opts)
