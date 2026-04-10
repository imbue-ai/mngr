from collections.abc import Sequence
from pathlib import Path
from subprocess import TimeoutExpired

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr_kanpan.data_source import CommitsAheadField
from imbue.mngr_kanpan.data_source import FIELD_COMMITS_AHEAD
from imbue.mngr_kanpan.data_source import FieldValue


class GitInfoDataSource:
    """Computes commits_ahead field from git rev-list --count."""

    @property
    def name(self) -> str:
        return "git_info"

    @property
    def columns(self) -> dict[str, str]:
        return {FIELD_COMMITS_AHEAD: "GIT"}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {FIELD_COMMITS_AHEAD: CommitsAheadField}

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        cg = mngr_ctx.concurrency_group

        # Collect local work dirs for agents
        agent_work_dirs: dict[AgentName, Path] = {}
        for agent in agents:
            if agent.host.provider_name == LOCAL_PROVIDER_NAME and agent.work_dir.exists():
                agent_work_dirs[agent.name] = agent.work_dir

        # Get commits-ahead counts for all unique dirs in parallel
        commits_ahead_map = _get_all_commits_ahead(list(set(agent_work_dirs.values())), cg)

        fields: dict[AgentName, dict[str, FieldValue]] = {}
        for agent in agents:
            work_dir = agent_work_dirs.get(agent.name)
            if work_dir is not None:
                count = commits_ahead_map.get(work_dir)
                fields[agent.name] = {
                    FIELD_COMMITS_AHEAD: CommitsAheadField(count=count, has_work_dir=True),
                }
            else:
                fields[agent.name] = {
                    FIELD_COMMITS_AHEAD: CommitsAheadField(count=None, has_work_dir=False),
                }

        return fields, []


def _get_all_commits_ahead(
    work_dirs: list[Path],
    cg: ConcurrencyGroup,
) -> dict[Path, int | None]:
    """Get commits-ahead counts for multiple work dirs in parallel."""
    if not work_dirs:
        return {}

    unique_dirs = set(work_dirs)
    result: dict[Path, int | None] = {}
    processes: list[tuple[Path, RunningProcess]] = []
    for work_dir in unique_dirs:
        try:
            proc = cg.run_process_in_background(
                ["git", "rev-list", "--count", "@{upstream}..HEAD"],
                cwd=work_dir,
                timeout=10.0,
                is_checked_by_group=False,
            )
        except (ConcurrencyGroupError, OSError) as exc:
            logger.debug("Failed to launch git rev-list in {}: {}", work_dir, exc)
            result[work_dir] = None
            continue
        processes.append((work_dir, proc))

    for work_dir, proc in processes:
        try:
            proc.wait()
        except (ConcurrencyGroupError, TimeoutExpired) as exc:
            logger.debug("git rev-list failed in {}: {}", work_dir, exc)
            result[work_dir] = None
            continue
        if proc.returncode == 0:
            try:
                result[work_dir] = int(proc.read_stdout().strip())
            except ValueError as exc:
                logger.debug("Unparseable git rev-list output in {}: {}", work_dir, exc)
                result[work_dir] = None
        else:
            logger.debug("git rev-list exited with code {} in {}", proc.returncode, work_dir)
            result[work_dir] = None
    return result
