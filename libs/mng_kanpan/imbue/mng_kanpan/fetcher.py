import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.pure import pure
from imbue.mng.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mng.api.list import list_agents
from imbue.mng.api.list import load_all_agents_grouped_by_host
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import LOCAL_PROVIDER_NAME
from imbue.mng.utils.git_utils import get_current_git_branch
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.github import fetch_all_prs

PLUGIN_NAME = "kanpan"


def fetch_board_snapshot(mng_ctx: MngContext) -> BoardSnapshot:
    """Fetch a complete board snapshot: agents, branches, and PR associations.

    Lists all agents, fetches GitHub PRs, resolves each agent's branch,
    and matches agents to PRs by branch name.
    """
    start_time = time.monotonic()
    errors: list[str] = []
    cg = mng_ctx.concurrency_group

    # List all agents (continue on errors to show partial results)
    result = list_agents(mng_ctx, is_streaming=False, error_behavior=ErrorBehavior.CONTINUE)
    for error in result.errors:
        errors.append(f"{error.exception_type}: {error.message}")

    # Load agent references to read plugin data (certified_data from data.json)
    muted_agents = _load_muted_agents(mng_ctx)

    # Find a local agent work_dir to use as cwd for gh (so it can detect the repo)
    gh_cwd = _find_git_cwd(result.agents)

    # Fetch all PRs from GitHub
    pr_result = fetch_all_prs(cg, cwd=gh_cwd)
    if pr_result.error is not None:
        errors.append(pr_result.error)
    pr_by_branch = _build_pr_branch_index(pr_result.prs)

    # Detect GitHub repo for PR creation URLs
    repo_path = _get_github_repo_path(gh_cwd, cg) if gh_cwd is not None else None

    # Build board entries with branch and PR info
    entries: list[AgentBoardEntry] = []
    for agent in result.agents:
        branch = _resolve_agent_branch(agent, cg)
        pr = pr_by_branch.get(branch) if branch else None
        is_local = agent.host.provider_name == LOCAL_PROVIDER_NAME
        local_work_dir = agent.work_dir if is_local and agent.work_dir.exists() else None
        commits_ahead = _get_commits_ahead(local_work_dir, cg) if local_work_dir is not None else None
        create_pr_url = _build_create_pr_url(repo_path, branch) if repo_path and branch and pr is None else None
        entries.append(
            AgentBoardEntry(
                name=agent.name,
                state=agent.state,
                provider_name=agent.host.provider_name,
                work_dir=local_work_dir,
                branch=branch,
                pr=pr,
                commits_ahead=commits_ahead,
                create_pr_url=create_pr_url,
                is_muted=agent.name in muted_agents,
            )
        )

    elapsed = time.monotonic() - start_time
    return BoardSnapshot(
        entries=tuple(entries),
        errors=tuple(errors),
        fetch_time_seconds=elapsed,
    )


def toggle_agent_mute(mng_ctx: MngContext, agent_name: AgentName) -> bool:
    """Toggle the mute state of an agent. Returns the new mute state."""
    agents_by_host, _ = load_all_agents_grouped_by_host(mng_ctx)
    agent, _host = find_and_maybe_start_agent_by_name_or_id(
        str(agent_name),
        agents_by_host,
        mng_ctx,
        command_name="kanpan",
        skip_agent_state_check=True,
    )
    plugin_data = agent.get_plugin_data(PLUGIN_NAME)
    is_muted = not plugin_data.get("muted", False)
    plugin_data["muted"] = is_muted
    agent.set_plugin_data(PLUGIN_NAME, plugin_data)
    return is_muted


def _load_muted_agents(mng_ctx: MngContext) -> set[AgentName]:
    """Load the set of muted agent names from plugin data."""
    muted: set[AgentName] = set()
    try:
        agents_by_host, _ = load_all_agents_grouped_by_host(mng_ctx)
        for _host_ref, agent_refs in agents_by_host.items():
            for agent_ref in agent_refs:
                plugin_data: dict[str, Any] = agent_ref.certified_data.get("plugin", {}).get(PLUGIN_NAME, {})
                if plugin_data.get("muted", False):
                    muted.add(agent_ref.agent_name)
    except Exception as e:
        logger.debug("Failed to load muted agents: {}", e)
    return muted


def _find_git_cwd(agents: list[AgentInfo]) -> Path | None:
    """Find a local agent work_dir to use as cwd for gh commands.

    Returns the first accessible local agent work_dir, or None if no local
    agent has an accessible work_dir.
    """
    for agent in agents:
        if agent.host.provider_name == LOCAL_PROVIDER_NAME and agent.work_dir.exists():
            return agent.work_dir
    return None


def _resolve_agent_branch(agent: AgentInfo, cg: ConcurrencyGroup) -> str | None:
    """Determine the git branch associated with an agent.

    For local agents with an accessible work_dir, reads the branch via git.
    Falls back to the naming convention mng/<name>-<provider>.
    """
    if agent.host.provider_name == LOCAL_PROVIDER_NAME:
        work_dir = agent.work_dir
        if work_dir.exists():
            branch = get_current_git_branch(work_dir, cg)
            if branch is not None:
                return branch
            logger.debug("Could not determine git branch for agent {} at {}", agent.name, work_dir)

    # Fallback: naming convention
    return f"mng/{agent.name}-{agent.host.provider_name}"


def _get_commits_ahead(work_dir: Path | None, cg: ConcurrencyGroup) -> int | None:
    """Get the number of commits the local branch is ahead of its remote tracking branch.

    Returns None if no upstream is configured or the check fails.
    Returns 0 if the branch is up to date with the remote.
    """
    if work_dir is None:
        return None
    try:
        result = cg.run_process_to_completion(
            ["git", "rev-list", "--count", "@{upstream}..HEAD"],
            cwd=work_dir,
        )
        return int(result.stdout.strip())
    except (ProcessError, ValueError):
        return None


def _get_github_repo_path(work_dir: Path, cg: ConcurrencyGroup) -> str | None:
    """Get the GitHub owner/repo path from a git repository's remote URL.

    Returns a string like "owner/repo", or None if the remote is not GitHub
    or cannot be determined.
    """
    try:
        result = cg.run_process_to_completion(
            ["git", "remote", "get-url", "origin"],
            cwd=work_dir,
        )
        return _parse_github_repo_path(result.stdout.strip())
    except ProcessError:
        return None


@pure
def _parse_github_repo_path(remote_url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL.

    Supports SSH (git@github.com:owner/repo.git) and
    HTTPS (https://github.com/owner/repo.git) formats.
    """
    # SSH format: git@github.com:owner/repo.git
    if remote_url.startswith("git@github.com:"):
        path = remote_url[len("git@github.com:") :]
        if path.endswith(".git"):
            path = path[:-4]
        return path

    # HTTPS format: https://github.com/owner/repo.git
    parsed = urlparse(remote_url)
    if parsed.hostname == "github.com":
        path = parsed.path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return path

    return None


@pure
def _build_create_pr_url(repo_path: str | None, branch: str | None) -> str | None:
    """Build a GitHub URL for creating a new PR from the given branch.

    Returns None if repo_path or branch is not available.
    """
    if repo_path is None or branch is None:
        return None
    return f"https://github.com/{repo_path}/compare/{branch}?expand=1"


@pure
def _build_pr_branch_index(prs: tuple[PrInfo, ...]) -> dict[str, PrInfo]:
    """Build a lookup dict from branch name to the most relevant PR.

    If multiple PRs share the same branch, prefers OPEN > MERGED > CLOSED.
    """
    result: dict[str, PrInfo] = {}
    for pr in prs:
        existing = result.get(pr.head_branch)
        if existing is None or _pr_priority(pr) > _pr_priority(existing):
            result[pr.head_branch] = pr
    return result


@pure
def _pr_priority(pr: PrInfo) -> int:
    """Return priority for PR selection when multiple PRs share a branch.

    Higher value means higher priority. OPEN > MERGED > CLOSED.
    """
    if pr.state == PrState.OPEN:
        return 2
    if pr.state == PrState.MERGED:
        return 1
    return 0
