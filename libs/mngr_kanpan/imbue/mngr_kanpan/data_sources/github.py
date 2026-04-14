import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import auto
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_CONFLICTS
from imbue.mngr_kanpan.data_source import FIELD_CREATE_PR_URL
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_source import FIELD_UNRESOLVED
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.data_sources.repo_paths import repo_path_from_labels

_BASE_FIELDS = "number,title,state,headRefName,url,isDraft"
_OPEN_FIELDS = f"{_BASE_FIELDS},statusCheckRollup"


class PrState(UpperCaseStrEnum):
    """State of a GitHub pull request."""

    OPEN = auto()
    CLOSED = auto()
    MERGED = auto()


class CiStatus(UpperCaseStrEnum):
    """Aggregate CI check status for a PR."""

    PASSING = auto()
    FAILING = auto()
    PENDING = auto()
    UNKNOWN = auto()

    @property
    def color(self) -> str | None:
        return {
            CiStatus.PASSING: "light green",
            CiStatus.FAILING: "light red",
            CiStatus.PENDING: "yellow",
        }.get(self)


class PrField(FieldValue):
    """GitHub pull request field value."""

    number: int = Field(description="PR number")
    url: str = Field(description="PR URL")
    is_draft: bool = Field(description="Whether the PR is a draft")
    title: str = Field(description="PR title")
    state: PrState = Field(description="PR state (open/closed/merged)")
    head_branch: str = Field(description="Head branch name of the PR")

    def display(self) -> CellDisplay:
        return CellDisplay(text=f"#{self.number}", url=self.url)


class CiField(FieldValue):
    """CI check status field value."""

    status: CiStatus = Field(description="Aggregate CI check status")

    def display(self) -> CellDisplay:
        if self.status == CiStatus.UNKNOWN:
            return CellDisplay(text="")
        return CellDisplay(text=self.status.lower(), color=self.status.color)


class CreatePrUrlField(FieldValue):
    """URL to create a new PR for a branch."""

    url: str = Field(description="URL to create a PR")

    def display(self) -> CellDisplay:
        return CellDisplay(text="+PR", url=self.url)


class ConflictsField(FieldValue):
    """Merge conflict status for a PR."""

    has_conflicts: bool = Field(description="Whether the PR has merge conflicts")

    def display(self) -> CellDisplay:
        if self.has_conflicts:
            return CellDisplay(text="YES", color="light red")
        return CellDisplay(text="no", color="light green")


class UnresolvedField(FieldValue):
    """Unresolved review comment status for a PR."""

    has_unresolved: bool = Field(description="Whether the PR has unresolved review comments")

    def display(self) -> CellDisplay:
        if self.has_unresolved:
            return CellDisplay(text="YES", color="light red")
        return CellDisplay(text="no", color="light green")


class PrInfo(FrozenModel):
    """GitHub pull request information from the gh CLI.

    This is the raw data structure used internally by the fetch layer.
    Data sources convert this to PrField for the board.
    """

    number: int = Field(description="PR number")
    title: str = Field(description="PR title")
    state: PrState = Field(description="PR state (open/closed/merged)")
    url: str = Field(description="PR URL")
    head_branch: str = Field(description="Head branch name of the PR")
    check_status: CiStatus = Field(description="Aggregate CI check status")
    is_draft: bool = Field(description="Whether the PR is a draft")


class FetchPrsResult(FrozenModel):
    """Result of fetching PRs from GitHub."""

    prs: tuple[PrInfo, ...] = Field(description="Fetched PRs")
    error: str | None = Field(default=None, description="Error message if fetch failed")


def _build_gh_pr_list_cmd(
    state: str,
    fields: str,
    limit: int,
    repo: str | None,
) -> list[str]:
    """Build a gh pr list command with the given parameters."""
    cmd = ["gh", "pr", "list", "--author", "@me", "--state", state, "--json", fields, "--limit", str(limit)]
    if repo is not None:
        cmd.extend(["--repo", repo])
    return cmd


def fetch_all_prs(cg: ConcurrencyGroup, cwd: Path | None = None, repo: str | None = None) -> FetchPrsResult:
    """Fetch PRs from a repo using gh CLI, in two parallel passes.

    Repo is identified by repo ('owner/repo' string passed via --repo) or
    by cwd (a directory inside the target git repository). Prefer repo
    since it doesn't require a local checkout.

    Pass 1: open PRs with statusCheckRollup (CI check data). This is a small
    result set where CI status matters.

    Pass 2: all PRs without statusCheckRollup (lightweight metadata only). This
    provides branch matching for closed/merged PRs without the expensive CI
    status resolution that causes GitHub API 504 timeouts.
    """
    try:
        open_proc = cg.run_process_in_background(
            _build_gh_pr_list_cmd("open", _OPEN_FIELDS, 100, repo),
            timeout=30,
            cwd=cwd,
            is_checked_by_group=False,
        )
        all_proc = cg.run_process_in_background(
            _build_gh_pr_list_cmd("all", _BASE_FIELDS, 500, repo),
            timeout=30,
            cwd=cwd,
            is_checked_by_group=False,
        )

        open_proc.wait()
        all_proc.wait()
    except (ProcessError, OSError) as e:
        logger.debug("Failed to launch gh pr list: {}", e)
        return FetchPrsResult(prs=(), error=f"gh pr list failed: {e}")

    errors: list[str] = []
    prs_by_number: dict[int, PrInfo] = {}

    # Open PRs first (have CI status data from statusCheckRollup)
    open_parsed = _parse_gh_output(
        open_proc.read_stdout(),
        open_proc.returncode,
        open_proc.read_stderr(),
    )
    match open_parsed:
        case str(err):
            errors.append(f"open: {err}")
        case list(raw_prs):
            for raw in raw_prs:
                pr = _parse_pr(raw)
                prs_by_number[pr.number] = pr

    # Closed/merged PRs from the all-states query (no CI status)
    all_parsed = _parse_gh_output(
        all_proc.read_stdout(),
        all_proc.returncode,
        all_proc.read_stderr(),
    )
    match all_parsed:
        case str(err):
            errors.append(f"all: {err}")
        case list(raw_prs):
            for raw in raw_prs:
                pr = _parse_pr(raw)
                if pr.number not in prs_by_number:
                    prs_by_number[pr.number] = pr

    if not prs_by_number and errors:
        return FetchPrsResult(prs=(), error=f"gh pr list failed ({'; '.join(errors)})")

    return FetchPrsResult(prs=tuple(prs_by_number.values()), error=None)


def _parse_gh_output(
    stdout: str,
    returncode: int | None,
    stderr: str,
) -> list[dict[str, Any]] | str:
    """Parse gh CLI process output into raw PR dicts, or return an error string."""
    if returncode is not None and returncode != 0:
        return stderr.strip() or stdout.strip() or f"exit code {returncode}"
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as e:
        return f"parse error: {e}"


@pure
def _parse_pr(raw: dict[str, Any]) -> PrInfo:
    """Parse a single raw PR dict from gh CLI JSON output into PrInfo."""
    return PrInfo(
        number=raw["number"],
        title=raw["title"],
        state=_parse_pr_state(raw["state"]),
        url=raw["url"],
        head_branch=raw["headRefName"],
        check_status=_parse_check_status(raw.get("statusCheckRollup")),
        is_draft=bool(raw.get("isDraft", False)),
    )


@pure
def _parse_pr_state(state_str: str) -> PrState:
    """Convert gh CLI state string to PrState enum."""
    upper = state_str.upper()
    if upper == "MERGED":
        return PrState.MERGED
    if upper == "CLOSED":
        return PrState.CLOSED
    return PrState.OPEN


@pure
def _parse_check_status(rollup: list[dict[str, Any]] | None) -> CiStatus:
    """Derive aggregate check status from statusCheckRollup.

    Priority: any failure -> FAILING, any pending -> PENDING,
    all success -> PASSING, empty/None -> UNKNOWN.
    """
    if not rollup:
        return CiStatus.UNKNOWN

    has_pending = False
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()

        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            return CiStatus.FAILING
        if status != "COMPLETED":
            has_pending = True

    if has_pending:
        return CiStatus.PENDING
    return CiStatus.PASSING


class GitHubDataSourceConfig(FrozenModel):
    """Configuration for the GitHub data source."""

    pr: bool = Field(default=True, description="Fetch PR number/URL/state/draft")
    ci: bool = Field(default=True, description="Fetch CI check status")
    create_pr_url: bool = Field(default=True, description="Generate URL to create PR if none exists")
    conflicts: bool = Field(default=True, description="Check merge conflict status via gh pr view")
    unresolved: bool = Field(default=True, description="Check unresolved PR comments via GraphQL")
    unresolved_ignore_user: str | None = Field(
        default=None,
        description="GitHub username whose review threads to ignore when checking for unresolved comments. "
        "Threads where the last comment is by this user are skipped (you already replied).",
    )


_DEFAULT_CONFIG = GitHubDataSourceConfig()


class GitHubDataSource(FrozenModel):
    """Fetches GitHub PR, CI, conflict, and unresolved comment data.

    Uses the gh CLI and GitHub GraphQL API. Reads repo_path from cached fields
    (produced by RepoPathsDataSource in the previous cycle).
    """

    config: GitHubDataSourceConfig = Field(default_factory=GitHubDataSourceConfig)

    @property
    def name(self) -> str:
        return "github"

    @property
    def is_remote(self) -> bool:
        return True

    @property
    def columns(self) -> dict[str, str]:
        cols: dict[str, str] = {}
        if self.config.pr:
            cols[FIELD_PR] = "PR"
        if self.config.ci:
            cols[FIELD_CI] = "CI"
        if self.config.create_pr_url:
            cols[FIELD_CREATE_PR_URL] = ""
        if self.config.conflicts:
            cols[FIELD_CONFLICTS] = "CONFLICTS"
        if self.config.unresolved:
            cols[FIELD_UNRESOLVED] = "UNRESOLVED"
        return cols

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        types: dict[str, type[FieldValue]] = {}
        if self.config.pr:
            types[FIELD_PR] = PrField
        if self.config.ci:
            types[FIELD_CI] = CiField
        if self.config.create_pr_url:
            types[FIELD_CREATE_PR_URL] = CreatePrUrlField
        if self.config.conflicts:
            types[FIELD_CONFLICTS] = ConflictsField
        if self.config.unresolved:
            types[FIELD_UNRESOLVED] = UnresolvedField
        return types

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        cg = mngr_ctx.concurrency_group
        errors: list[str] = []

        # Resolve repo paths: prefer cached (from previous cycle), fall back to labels
        agent_repos: dict[AgentName, str] = {}
        for agent in agents:
            repo_path = _get_cached_repo_path(cached_fields, agent.name)
            if repo_path is None:
                repo_path = repo_path_from_labels(agent.labels)
            if repo_path is not None:
                agent_repos[agent.name] = repo_path

        # Collect unique repos
        all_repos: set[str] = set(agent_repos.values())
        if not all_repos:
            return {}, errors

        # Fetch PRs for all unique repos in parallel
        pr_by_repo_branch: dict[str, dict[str, _PrFieldInternal]] = {}
        repo_pr_loaded: dict[str, bool] = {}

        with ThreadPoolExecutor(max_workers=min(len(all_repos), 8)) as executor:
            for repo_path, pr_result in executor.map(lambda rp: _fetch_repo_prs(cg, rp), all_repos):
                if pr_result.error is None:
                    repo_index = _build_pr_branch_index(pr_result.prs)
                    if repo_index:
                        pr_by_repo_branch[repo_path] = repo_index
                    repo_pr_loaded[repo_path] = True
                else:
                    repo_pr_loaded[repo_path] = False
                    errors.append(pr_result.error)

        # Build agent fields
        fields: dict[AgentName, dict[str, FieldValue]] = {}
        for agent in agents:
            agent_repo = agent_repos.get(agent.name)
            branch = agent.initial_branch
            agent_fields: dict[str, FieldValue] = {}

            if agent_repo is not None and branch is not None:
                pr = _lookup_pr(pr_by_repo_branch, agent_repo, branch)
                agent_prs_loaded = repo_pr_loaded.get(agent_repo) is True

                if self.config.pr and pr is not None:
                    agent_fields[FIELD_PR] = pr
                if self.config.ci and pr is not None:
                    agent_fields[FIELD_CI] = CiField(status=pr.internal_check_status)
                if self.config.create_pr_url and agent_prs_loaded and pr is None:
                    agent_fields[FIELD_CREATE_PR_URL] = CreatePrUrlField(url=_build_create_pr_url(agent_repo, branch))

            if agent_fields:
                fields[agent.name] = agent_fields

        # Fetch conflicts and unresolved in a second pass (requires PR numbers)
        if self.config.conflicts or self.config.unresolved:
            extra_fields, extra_errors = _fetch_pr_metadata(cg, agents, fields, agent_repos, self.config)
            for agent_name, extra in extra_fields.items():
                if agent_name in fields:
                    fields[agent_name].update(extra)
                else:
                    fields[agent_name] = extra
            errors.extend(extra_errors)

        return fields, errors


class _PrFieldInternal(PrField):
    """Internal PR field with check_status for CI field extraction."""

    internal_check_status: CiStatus = Field(default=CiStatus.UNKNOWN, description="CI check status for internal use")


def _get_cached_repo_path(cached_fields: dict[AgentName, dict[str, FieldValue]], agent_name: AgentName) -> str | None:
    """Get repo path from cached fields if available."""
    agent_cached = cached_fields.get(agent_name)
    if agent_cached is None:
        return None
    repo_field = agent_cached.get(FIELD_REPO_PATH)
    if isinstance(repo_field, RepoPathField):
        return repo_field.path
    return None


def _fetch_repo_prs(cg: ConcurrencyGroup, repo_path: str) -> tuple[str, "_FetchPrsResult"]:
    """Fetch PRs for a single repo."""
    result = fetch_all_prs(cg, repo=repo_path)
    # Convert PrInfo objects to PrField objects
    pr_fields: list[_PrFieldInternal] = []
    for pr_info in result.prs:
        pr_fields.append(
            _PrFieldInternal(
                number=pr_info.number,
                url=pr_info.url,
                is_draft=pr_info.is_draft,
                title=pr_info.title,
                state=PrState(str(pr_info.state)),
                head_branch=pr_info.head_branch,
                internal_check_status=CiStatus(str(pr_info.check_status)),
            )
        )
    return repo_path, _FetchPrsResult(prs=tuple(pr_fields), error=result.error)


class _FetchPrsResult(FrozenModel):
    """Result of fetching PRs from GitHub, using PrField."""

    prs: tuple[_PrFieldInternal, ...] = Field(description="Fetched PRs as PrField objects")
    error: str | None = Field(default=None, description="Error message if fetch failed")


@pure
def _build_pr_branch_index(prs: tuple[_PrFieldInternal, ...]) -> dict[str, _PrFieldInternal]:
    """Build a lookup dict from branch name to the most relevant PR.

    If multiple PRs share the same branch, prefers OPEN > MERGED > CLOSED.
    """
    result: dict[str, _PrFieldInternal] = {}
    for pr in prs:
        existing = result.get(pr.head_branch)
        if existing is None or _pr_priority(pr) > _pr_priority(existing):
            result[pr.head_branch] = pr
    return result


@pure
def _pr_priority(pr: PrField) -> int:
    """Return priority for PR selection. OPEN > MERGED > CLOSED."""
    if pr.state == PrState.OPEN:
        return 2
    if pr.state == PrState.MERGED:
        return 1
    return 0


@pure
def _lookup_pr(
    pr_by_repo_branch: dict[str, dict[str, _PrFieldInternal]],
    agent_repo: str,
    branch: str,
) -> _PrFieldInternal | None:
    """Look up the PR for an agent by its repo and branch."""
    repo_prs = pr_by_repo_branch.get(agent_repo)
    return repo_prs.get(branch) if repo_prs is not None else None


@pure
def _build_create_pr_url(repo_path: str, branch: str) -> str:
    """Build a GitHub URL for creating a new PR from the given branch."""
    return f"https://github.com/{repo_path}/compare/{branch}?expand=1"


def _fetch_pr_metadata(
    cg: ConcurrencyGroup,
    agents: tuple[AgentDetails, ...],
    fields: dict[AgentName, dict[str, FieldValue]],
    agent_repos: dict[AgentName, str],
    config: GitHubDataSourceConfig,
) -> tuple[dict[AgentName, dict[str, FieldValue]], list[str]]:
    """Fetch conflicts and unresolved comments for agents that have PRs."""
    errors: list[str] = []
    extra_fields: dict[AgentName, dict[str, FieldValue]] = {}

    # Collect agents with open PRs
    agents_with_prs: list[tuple[AgentName, str, int]] = []
    for agent in agents:
        agent_f = fields.get(agent.name, {})
        pr = agent_f.get(FIELD_PR)
        if isinstance(pr, PrField) and pr.state == PrState.OPEN:
            repo = agent_repos.get(agent.name)
            if repo is not None:
                agents_with_prs.append((agent.name, repo, pr.number))

    if not agents_with_prs:
        return extra_fields, errors

    # Fetch conflicts and unresolved in parallel per agent
    processes: list[tuple[AgentName, str, int, RunningProcess | None, RunningProcess | None]] = []

    for agent_name, repo, pr_number in agents_with_prs:
        conflict_proc: RunningProcess | None = None
        unresolved_proc: RunningProcess | None = None

        if config.conflicts:
            try:
                conflict_proc = cg.run_process_in_background(
                    ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "mergeable"],
                    timeout=15.0,
                    is_checked_by_group=False,
                )
            except (ProcessError, OSError) as e:
                logger.debug("Failed to launch gh pr view for conflicts: {}", e)

        if config.unresolved:
            try:
                query = _build_unresolved_query(repo, pr_number)
                unresolved_proc = cg.run_process_in_background(
                    ["gh", "api", "graphql", "-f", f"query={query}"],
                    timeout=15.0,
                    is_checked_by_group=False,
                )
            except (ProcessError, OSError) as e:
                logger.debug("Failed to launch gh api graphql for unresolved: {}", e)

        processes.append((agent_name, repo, pr_number, conflict_proc, unresolved_proc))

    for agent_name, repo, pr_number, conflict_proc, unresolved_proc in processes:
        agent_extra: dict[str, FieldValue] = {}

        if conflict_proc is not None:
            try:
                conflict_proc.wait()
                if conflict_proc.returncode == 0:
                    stdout = conflict_proc.read_stdout()
                    has_conflicts = _parse_conflicts(stdout)
                    agent_extra[FIELD_CONFLICTS] = ConflictsField(has_conflicts=has_conflicts)
            except Exception as e:
                logger.debug("Failed to check conflicts for PR #{} in {}: {}", pr_number, repo, e)

        if unresolved_proc is not None:
            try:
                unresolved_proc.wait()
                if unresolved_proc.returncode == 0:
                    stdout = unresolved_proc.read_stdout()
                    has_unresolved = _parse_unresolved(stdout, ignore_user=config.unresolved_ignore_user)
                    agent_extra[FIELD_UNRESOLVED] = UnresolvedField(has_unresolved=has_unresolved)
            except Exception as e:
                logger.debug("Failed to check unresolved for PR #{} in {}: {}", pr_number, repo, e)

        if agent_extra:
            extra_fields[agent_name] = agent_extra

    return extra_fields, errors


def _parse_conflicts(stdout: str) -> bool:
    """Parse gh pr view --json mergeable output to determine conflict status."""
    try:
        data = json.loads(stdout)
        return data.get("mergeable") == "CONFLICTING"
    except (json.JSONDecodeError, TypeError):
        return False


def _build_unresolved_query(repo: str, pr_number: int) -> str:
    """Build a GraphQL query to check for unresolved review threads."""
    owner, name = repo.split("/", 1)
    return (
        '{ repository(owner: "%s", name: "%s") '
        "{ pullRequest(number: %d) "
        "{ reviewThreads(first: 100) { nodes { isResolved "
        "comments(last: 1) { nodes { author { login } } } } } } } }"
    ) % (owner, name, pr_number)


def _parse_unresolved(stdout: str, ignore_user: str | None = None) -> bool:
    """Parse GraphQL response to determine if there are unresolved review threads.

    If ignore_user is set, threads where the last comment's author matches
    that username are skipped (you already replied, ball is in their court).
    """
    try:
        data = json.loads(stdout)
        threads = (
            data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])
        )
        for thread in threads:
            if thread.get("isResolved", True):
                continue
            if ignore_user is not None:
                comments = thread.get("comments", {}).get("nodes", [])
                if comments:
                    author = comments[0].get("author", {}).get("login")
                    if author == ignore_user:
                        continue
            return True
        return False
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False
