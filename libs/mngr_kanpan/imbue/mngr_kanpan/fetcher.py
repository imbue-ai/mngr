import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_all_hosts_and_agents
from imbue.mngr.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import ColumnData
from imbue.mngr_kanpan.data_types import GitHubData
from imbue.mngr_kanpan.data_types import PrInfo
from imbue.mngr_kanpan.data_types import PrState
from imbue.mngr_kanpan.data_types import RefreshHook
from imbue.mngr_kanpan.github import fetch_all_prs

PLUGIN_NAME = "kanpan"


def fetch_agent_snapshot(
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> BoardSnapshot:
    """Fetch agent state: agents, git branches, commits ahead, mute state.

    Entries have pr=None and create_pr_url=None (no GitHub API calls).
    """
    start_time = time.monotonic()
    errors: list[str] = []
    cg = mngr_ctx.concurrency_group

    result = list_agents(
        mngr_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )
    for error in result.errors:
        errors.append(f"{error.exception_type}: {error.message}")

    muted_agents = _load_muted_agents(mngr_ctx)

    entries: list[AgentBoardEntry] = []
    for agent in result.agents:
        branch = agent.initial_branch
        is_local = agent.host.provider_name == LOCAL_PROVIDER_NAME
        local_work_dir = agent.work_dir if is_local and agent.work_dir.exists() else None
        commits_ahead = _get_commits_ahead(local_work_dir, cg) if local_work_dir is not None else None
        entries.append(
            AgentBoardEntry(
                name=agent.name,
                state=agent.state,
                provider_name=agent.host.provider_name,
                work_dir=local_work_dir,
                branch=branch,
                commits_ahead=commits_ahead,
                is_muted=agent.name in muted_agents,
                column_data=ColumnData(
                    labels=agent.labels,
                    plugin_data=agent.plugin,
                ),
            )
        )

    elapsed = time.monotonic() - start_time
    return BoardSnapshot(
        entries=tuple(entries),
        errors=tuple(errors),
        prs_loaded=False,
        fetch_time_seconds=elapsed,
    )


def fetch_github_data(mngr_ctx: MngrContext, agents: list[AgentDetails]) -> GitHubData:
    """Fetch GitHub PR data and build the PR-to-branch index.

    Returns a GitHubData containing pr_by_branch, repo_path, and any errors.
    """
    cg = mngr_ctx.concurrency_group
    errors: list[str] = []

    gh_cwd = _find_git_cwd(agents)

    pr_result = fetch_all_prs(cg, cwd=gh_cwd)
    prs_loaded = pr_result.error is None
    if pr_result.error is not None:
        errors.append(pr_result.error)
    pr_by_branch = _build_pr_branch_index(pr_result.prs)

    repo_path = _get_github_repo_path(gh_cwd, cg) if gh_cwd is not None else None

    return GitHubData(
        pr_by_branch=pr_by_branch,
        repo_path=repo_path,
        prs_loaded=prs_loaded,
        errors=tuple(errors),
    )


@pure
def enrich_snapshot_with_github_data(snapshot: BoardSnapshot, remote: GitHubData) -> BoardSnapshot:
    """Enrich a local-only snapshot with GitHub PR data.

    For each entry, looks up PR by branch name and attaches pr and create_pr_url.
    """
    enriched_entries: list[AgentBoardEntry] = []
    for entry in snapshot.entries:
        pr = remote.pr_by_branch.get(entry.branch) if entry.branch else None
        create_pr_url = (
            _build_create_pr_url(remote.repo_path, entry.branch)
            if remote.prs_loaded and remote.repo_path and entry.branch and pr is None
            else None
        )
        enriched_entry = entry.model_copy_update(
            to_update(entry.field_ref().pr, pr),
            to_update(entry.field_ref().create_pr_url, create_pr_url),
        )
        enriched_entries.append(enriched_entry)

    return BoardSnapshot(
        entries=tuple(enriched_entries),
        errors=(*snapshot.errors, *remote.errors),
        prs_loaded=remote.prs_loaded,
        fetch_time_seconds=snapshot.fetch_time_seconds,
    )


def fetch_board_snapshot(
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...],
    exclude_filters: tuple[str, ...],
    on_before_refresh: list[RefreshHook] | None,
    on_after_refresh: list[RefreshHook] | None,
    prev_snapshot: BoardSnapshot | None,
) -> BoardSnapshot:
    """Full fetch: local snapshot enriched with GitHub PR data, with optional refresh hooks.

    Lists agents once and uses the result for both local and remote fetching.
    Before-hooks run against the previous snapshot's entries (skipped when prev_snapshot is None).
    After-hooks run against the new snapshot's entries.
    Hook errors are appended to the snapshot's errors but do not block the refresh.
    """
    start_time = time.monotonic()
    errors: list[str] = []
    cg = mngr_ctx.concurrency_group

    if prev_snapshot is not None and on_before_refresh:
        errors.extend(run_refresh_hooks(cg, on_before_refresh, prev_snapshot.entries))

    result = list_agents(
        mngr_ctx,
        is_streaming=False,
        error_behavior=ErrorBehavior.CONTINUE,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )
    for error in result.errors:
        errors.append(f"{error.exception_type}: {error.message}")

    muted_agents = _load_muted_agents(mngr_ctx)

    # Fetch remote data (GitHub PRs)
    remote = fetch_github_data(mngr_ctx, result.agents)

    # Build board entries with both local and remote info
    entries: list[AgentBoardEntry] = []
    for agent in result.agents:
        branch = agent.initial_branch
        is_local = agent.host.provider_name == LOCAL_PROVIDER_NAME
        local_work_dir = agent.work_dir if is_local and agent.work_dir.exists() else None
        commits_ahead = _get_commits_ahead(local_work_dir, cg) if local_work_dir is not None else None
        pr = remote.pr_by_branch.get(branch) if branch else None
        create_pr_url = (
            _build_create_pr_url(remote.repo_path, branch)
            if remote.prs_loaded and remote.repo_path and branch and pr is None
            else None
        )
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
                column_data=ColumnData(
                    labels=agent.labels,
                    plugin_data=agent.plugin,
                ),
            )
        )

    # fetch_time_seconds captures before-hooks + data fetch but not after-hooks,
    # because the snapshot (and its displayed timing) is constructed before
    # after-hooks run. Before-hooks are included since they can mutate state
    # that the fetch reads (e.g. clearing labels before re-fetching).
    snapshot = BoardSnapshot(
        entries=tuple(entries),
        errors=(*errors, *remote.errors),
        prs_loaded=remote.prs_loaded,
        fetch_time_seconds=time.monotonic() - start_time,
    )

    if on_after_refresh:
        after_errors = run_refresh_hooks(cg, on_after_refresh, snapshot.entries)
        if after_errors:
            snapshot = BoardSnapshot(
                entries=snapshot.entries,
                errors=(*snapshot.errors, *after_errors),
                prs_loaded=snapshot.prs_loaded,
                fetch_time_seconds=snapshot.fetch_time_seconds,
            )

    return snapshot


def run_refresh_hooks(
    cg: ConcurrencyGroup,
    hooks: list[RefreshHook],
    entries: tuple[AgentBoardEntry, ...],
) -> list[str]:
    """Run refresh hook commands for each agent in parallel. Returns list of error messages."""
    errors: list[str] = []
    for hook in hooks:
        processes: list[tuple[AgentBoardEntry, RunningProcess]] = []
        with cg.make_concurrency_group(name=f"hook-{hook.name}") as child_cg:
            for entry in entries:
                env = _build_hook_env(entry)
                proc = child_cg.run_process_in_background(
                    ["sh", "-c", hook.command],
                    timeout=30.0,
                    is_checked_by_group=False,
                    env=env,
                )
                processes.append((entry, proc))
        for entry, proc in processes:
            rc = proc.returncode
            if rc is not None and rc != 0:
                stderr = proc.read_stderr().strip()
                msg = f"Hook '{hook.name}' failed for {entry.name} (exit {rc})"
                if stderr:
                    msg = f"{msg}: {stderr}"
                errors.append(msg)
    return errors


def _build_hook_env(entry: AgentBoardEntry) -> dict[str, str]:
    """Build environment variables for a hook command from an agent board entry."""
    return {
        **os.environ,
        "MNGR_AGENT_NAME": str(entry.name),
        "MNGR_AGENT_BRANCH": entry.branch or "",
        "MNGR_AGENT_STATE": str(entry.state),
        "MNGR_AGENT_PROVIDER": str(entry.provider_name),
        "MNGR_AGENT_PR_NUMBER": str(entry.pr.number) if entry.pr else "",
        "MNGR_AGENT_PR_URL": entry.pr.url if entry.pr else "",
        "MNGR_AGENT_PR_STATE": str(entry.pr.state) if entry.pr else "",
    }


def toggle_agent_mute(mngr_ctx: MngrContext, agent_name: AgentName) -> bool:
    """Toggle the mute state of an agent. Returns the new mute state."""
    agents_by_host, _ = discover_all_hosts_and_agents(mngr_ctx)
    agent, _host = find_and_maybe_start_agent_by_name_or_id(
        str(agent_name),
        agents_by_host,
        mngr_ctx,
        command_name="kanpan",
        skip_agent_state_check=True,
    )
    plugin_data = agent.get_plugin_data(PLUGIN_NAME)
    is_muted = not plugin_data.get("muted", False)
    plugin_data["muted"] = is_muted
    agent.set_plugin_data(PLUGIN_NAME, plugin_data)
    return is_muted


def _load_muted_agents(mngr_ctx: MngrContext) -> set[AgentName]:
    """Load the set of muted agent names from certified data."""
    muted: set[AgentName] = set()
    try:
        agents_by_host, _providers = discover_all_hosts_and_agents(mngr_ctx)
        for _host_ref, agent_refs in agents_by_host.items():
            for agent_ref in agent_refs:
                if _is_agent_muted(agent_ref.certified_data):
                    muted.add(agent_ref.agent_name)
    except Exception as e:
        logger.debug("Failed to load muted agents: {}", e)
    return muted


def _is_agent_muted(certified_data: Any) -> bool:
    """Check if an agent is muted based on its certified data."""
    return certified_data.get("plugin", {}).get(PLUGIN_NAME, {}).get("muted", False)


def _find_git_cwd(agents: list[AgentDetails]) -> Path | None:
    """Find a local agent work_dir to use as cwd for gh commands.

    Returns the first accessible local agent work_dir, or None if no local
    agent has an accessible work_dir.
    """
    for agent in agents:
        if agent.host.provider_name == LOCAL_PROVIDER_NAME and agent.work_dir.exists():
            return agent.work_dir
    return None


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
