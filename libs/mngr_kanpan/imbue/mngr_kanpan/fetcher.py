import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discover import discover_hosts_and_agents
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
from imbue.mngr_kanpan.github import FetchPrsResult
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

    # Collect local work dirs for parallel commits-ahead check
    agent_work_dirs: dict[int, Path] = {}
    for i, agent in enumerate(result.agents):
        is_local = agent.host.provider_name == LOCAL_PROVIDER_NAME
        if is_local and agent.work_dir.exists():
            agent_work_dirs[i] = agent.work_dir
    commits_ahead_map = _get_all_commits_ahead(list(agent_work_dirs.values()), cg)

    entries: list[AgentBoardEntry] = []
    for i, agent in enumerate(result.agents):
        branch = agent.initial_branch
        local_work_dir = agent_work_dirs.get(i)
        commits_ahead = commits_ahead_map.get(local_work_dir) if local_work_dir is not None else None
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
        repo_pr_loaded={},
        fetch_time_seconds=elapsed,
    )


def fetch_github_data(mngr_ctx: MngrContext, agents: list[AgentDetails]) -> GitHubData:
    """Fetch GitHub PR data from all unique repos and build the PR-to-branch index.

    Discovers repos from each agent's 'remote' label (set at creation time).
    Fetches PRs once per unique repo via gh --repo (no local cwd needed).
    Agents without the 'remote' label are skipped.
    """
    cg = mngr_ctx.concurrency_group
    errors: list[str] = []

    # Collect unique repos from agent labels.
    all_repos: set[str] = set()
    for agent in agents:
        repo_path = _get_agent_repo_path(agent)
        if repo_path is not None:
            all_repos.add(repo_path)

    # Fetch PRs for all unique repos in parallel via gh --repo.
    pr_by_repo_branch: dict[str, dict[str, PrInfo]] = {}
    repo_pr_loaded: dict[str, bool] = {}

    def _fetch_repo(repo_path: str) -> tuple[str, FetchPrsResult]:
        return repo_path, fetch_all_prs(cg, repo=repo_path)

    with ThreadPoolExecutor(max_workers=min(len(all_repos), 8) or 1) as executor:
        for repo_path, pr_result in executor.map(_fetch_repo, all_repos):
            if pr_result.error is None:
                repo_index = _build_pr_branch_index(pr_result.prs)
                if repo_index:
                    pr_by_repo_branch[repo_path] = repo_index
                repo_pr_loaded[repo_path] = True
            else:
                repo_pr_loaded[repo_path] = False
                errors.append(pr_result.error)

    return GitHubData(
        pr_by_repo_branch=pr_by_repo_branch,
        repo_pr_loaded=repo_pr_loaded,
        errors=tuple(errors),
    )


@pure
def enrich_snapshot_with_github_data(snapshot: BoardSnapshot, remote: GitHubData) -> BoardSnapshot:
    """Enrich a local-only snapshot with GitHub PR data.

    For each entry, looks up PR by branch name and attaches pr and create_pr_url.
    create_pr_url is only generated for agents whose repo had a successful PR fetch.
    """
    enriched_entries: list[AgentBoardEntry] = []
    for entry in snapshot.entries:
        agent_repo = repo_path_from_labels(entry.column_data.labels)
        pr = _lookup_pr(remote, agent_repo, entry.branch)
        agent_prs_loaded = agent_repo is not None and remote.repo_pr_loaded.get(agent_repo) is True
        create_pr_url = (
            _build_create_pr_url(agent_repo, entry.branch)
            if agent_prs_loaded and entry.branch and pr is None
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
        repo_pr_loaded=remote.repo_pr_loaded,
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

    # Collect local work dirs for parallel commits-ahead check
    agent_work_dirs: dict[int, Path] = {}
    for i, agent in enumerate(result.agents):
        is_local = agent.host.provider_name == LOCAL_PROVIDER_NAME
        if is_local and agent.work_dir.exists():
            agent_work_dirs[i] = agent.work_dir
    commits_ahead_map = _get_all_commits_ahead(list(agent_work_dirs.values()), cg)

    # Build board entries with both local and remote info
    entries: list[AgentBoardEntry] = []
    for i, agent in enumerate(result.agents):
        branch = agent.initial_branch
        local_work_dir = agent_work_dirs.get(i)
        commits_ahead = commits_ahead_map.get(local_work_dir) if local_work_dir is not None else None
        agent_repo = _get_agent_repo_path(agent)
        pr = _lookup_pr(remote, agent_repo, branch)
        agent_prs_loaded = agent_repo is not None and remote.repo_pr_loaded.get(agent_repo) is True
        create_pr_url = (
            _build_create_pr_url(agent_repo, branch) if agent_prs_loaded and branch and pr is None else None
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
        repo_pr_loaded=remote.repo_pr_loaded,
        fetch_time_seconds=time.monotonic() - start_time,
    )

    if on_after_refresh:
        after_errors = run_refresh_hooks(cg, on_after_refresh, snapshot.entries)
        if after_errors:
            snapshot = snapshot.model_copy_update(
                to_update(snapshot.field_ref().errors, (*snapshot.errors, *after_errors)),
            )

    return snapshot


_HOOK_TIMEOUT_SECONDS = 30.0


def run_refresh_hooks(
    cg: ConcurrencyGroup,
    hooks: list[RefreshHook],
    entries: tuple[AgentBoardEntry, ...],
) -> list[str]:
    """Run refresh hook commands for each agent in parallel. Returns list of error messages.

    Hook failures (including timeouts) are collected as error strings and never propagate
    as exceptions -- callers always get their snapshot back.
    """
    errors: list[str] = []
    for hook in hooks:
        processes: list[tuple[AgentBoardEntry, RunningProcess]] = []
        try:
            with cg.make_concurrency_group(
                name=f"hook-{hook.name}",
                exit_timeout_seconds=_HOOK_TIMEOUT_SECONDS,
            ) as child_cg:
                for entry in entries:
                    env = _build_hook_env(entry)
                    proc = child_cg.run_process_in_background(
                        ["sh", "-c", hook.command],
                        timeout=_HOOK_TIMEOUT_SECONDS,
                        is_checked_by_group=False,
                        env=env,
                    )
                    processes.append((entry, proc))
        except ConcurrencyExceptionGroup as exc:
            n_failed = len(exc.exceptions)
            errors.append(f"Hook '{hook.name}': {n_failed} process(es) timed out or failed")
            logger.debug("Hook '{}' concurrency group error: {}", hook.name, exc)
            continue
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
    agents_by_host, _ = discover_hosts_and_agents(
        mngr_ctx,
        provider_names=None,
        agent_identifiers=(str(agent_name),),
        include_destroyed=False,
        reset_caches=False,
    )
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
        agents_by_host, _providers = discover_hosts_and_agents(
            mngr_ctx,
            provider_names=None,
            agent_identifiers=None,
            include_destroyed=False,
            reset_caches=False,
        )
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


def _get_all_commits_ahead(
    work_dirs: list[Path],
    cg: ConcurrencyGroup,
) -> dict[Path, int | None]:
    """Get commits-ahead counts for multiple work dirs in parallel.

    Launches all git rev-list processes concurrently, then collects results.
    Returns a dict mapping work_dir to commits-ahead count (None on failure).
    """
    if not work_dirs:
        return {}

    processes: list[tuple[Path, RunningProcess]] = []
    for work_dir in work_dirs:
        proc = cg.run_process_in_background(
            ["git", "rev-list", "--count", "@{upstream}..HEAD"],
            cwd=work_dir,
            timeout=10.0,
            is_checked_by_group=False,
        )
        processes.append((work_dir, proc))

    result: dict[Path, int | None] = {}
    for work_dir, proc in processes:
        proc.wait()
        if proc.returncode == 0:
            try:
                result[work_dir] = int(proc.read_stdout().strip())
            except ValueError:
                result[work_dir] = None
        else:
            result[work_dir] = None
    return result


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
def _get_agent_repo_path(agent: AgentDetails) -> str | None:
    """Get the GitHub repo path for an agent from its 'remote' label."""
    return repo_path_from_labels(agent.labels)


@pure
def repo_path_from_labels(labels: dict[str, str]) -> str | None:
    """Extract GitHub 'owner/repo' from a labels dict's 'remote' entry."""
    remote_url = labels.get("remote")
    if remote_url is None:
        return None
    return _parse_github_repo_path(remote_url)


@pure
def _lookup_pr(remote: GitHubData, agent_repo: str | None, branch: str | None) -> PrInfo | None:
    """Look up the PR for an agent by its repo and branch."""
    if not branch or agent_repo is None:
        return None
    repo_prs = remote.pr_by_repo_branch.get(agent_repo)
    return repo_prs.get(branch) if repo_prs is not None else None


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
