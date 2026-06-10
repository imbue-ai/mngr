import json
from collections.abc import Sequence
from datetime import datetime
from enum import auto
from typing import Annotated
from typing import Any
from typing import Final
from typing import Literal

from loguru import logger
from pydantic import Field
from pydantic import TypeAdapter
from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_CONFLICTS
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FIELD_REPO_PATH
from imbue.mngr_kanpan.data_source import FIELD_UNRESOLVED
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSourceError
from imbue.mngr_kanpan.data_source import now_utc
from imbue.mngr_kanpan.data_sources.repo_paths import RepoPathField
from imbue.mngr_kanpan.data_sources.repo_paths import repo_path_from_labels
from imbue.mngr_kanpan.data_types import DataSourceConfig

# GitHub's search connection caps `first` at 100 results per page, and the
# search API as a whole returns at most the first 1000 results (10 pages) for
# any one query. We paginate with cursors up to that ceiling.
_SEARCH_PAGE_SIZE: Final[int] = 100
_MAX_SEARCH_PAGES: Final[int] = 10

# A page request that comes back with no usable JSON body (HTTP 403 secondary
# rate limit, 5xx, transient network blip) is retried a few times with
# exponential backoff before we give up on it. Each retry re-requests the *same*
# page cursor, so pages already fetched are never re-fetched.
_PAGE_FETCH_ATTEMPTS: Final[int] = 3
_PAGE_RETRY_BASE_WAIT_SECONDS: Final[float] = 1.0
_PAGE_RETRY_MIN_WAIT_SECONDS: Final[float] = 1.0
_PAGE_RETRY_MAX_WAIT_SECONDS: Final[float] = 8.0
_STDERR_EXCERPT_LENGTH: Final[int] = 200


class GitHubBoardFetchError(KanpanDataSourceError):
    """Raised when a board page request comes back without a usable search result.

    Covers any response lacking a `data.s` object: an unparseable / empty body,
    an HTTP 403 secondary rate limit (whose REST-style error body has no
    `data.s`), a primary `RATE_LIMITED` GraphQL error (which nulls out `data`),
    or a 5xx. All are retried with a short backoff before giving up on the page.

    A primary `RATE_LIMITED` needs a multi-minute wait that the inline backoff
    cannot clear, so its retries simply exhaust quickly; the error is then
    surfaced and the board-level retry cooldown takes over on the next refresh.
    Earlier pages are preserved regardless of why this page failed.
    """

    ...


class PrState(UpperCaseStrEnum):
    """State of a GitHub pull request."""

    OPEN = auto()
    CLOSED = auto()
    MERGED = auto()


class CiStatus(UpperCaseStrEnum):
    """Aggregate CI check status for a PR.

    Values mirror GitHub's `StatusCheckRollup.state` enum so that
    `from_rollup_state` reduces to a straight `CiStatus(state)` lookup.
    `UNKNOWN` is our addition for the "no rollup at all" case.
    """

    SUCCESS = auto()
    FAILURE = auto()
    PENDING = auto()
    UNKNOWN = auto()

    @property
    def color(self) -> str | None:
        return {
            CiStatus.SUCCESS: "light green",
            CiStatus.FAILURE: "light red",
            CiStatus.PENDING: "yellow",
        }.get(self)

    @classmethod
    def from_rollup_state(cls, state: str | None) -> "CiStatus":
        """Map a `StatusCheckRollup.state` value (or `None`) to a `CiStatus`.

        Unknown / unmapped enum values fall back to `UNKNOWN` rather than
        raising, so a future GitHub-side enum addition doesn't crash the board.
        """
        if state is None:
            return cls.UNKNOWN
        try:
            return cls(state)
        except ValueError:
            return cls.UNKNOWN


class PrField(FieldValue):
    """GitHub pull request field value."""

    kind: Literal["pr"] = Field(default="pr", description="Discriminator tag")
    number: int = Field(description="PR number")
    url: str = Field(description="PR URL")
    is_draft: bool = Field(description="Whether the PR is a draft")
    title: str = Field(description="PR title")
    state: PrState = Field(description="PR state (open/closed/merged)")
    head_branch: str = Field(description="Head branch name of the PR")

    def display(self) -> CellDisplay:
        return CellDisplay(text=f"#{self.number}", url=self.url)

    def env_vars(self, key: str) -> dict[str, str]:
        return {
            "MNGR_FIELD_PR_NUMBER": str(self.number),
            "MNGR_FIELD_PR_URL": self.url,
            "MNGR_FIELD_PR_STATE": str(self.state),
        }


class CiField(FieldValue):
    """CI check status field value."""

    kind: Literal["ci"] = Field(default="ci", description="Discriminator tag")
    status: CiStatus = Field(description="Aggregate CI check status")

    def display(self) -> CellDisplay:
        if self.status == CiStatus.UNKNOWN:
            return CellDisplay(text="")
        return CellDisplay(text=self.status.lower(), color=self.status.color)

    def env_vars(self, key: str) -> dict[str, str]:
        return {"MNGR_FIELD_CI_STATUS": str(self.status)}


_CI_ADAPTER: TypeAdapter[FieldValue] = TypeAdapter(CiField)


class CreatePrUrlField(FieldValue):
    """URL to create a new PR for a branch."""

    kind: Literal["create_pr_url"] = Field(default="create_pr_url", description="Discriminator tag")
    url: str = Field(description="URL to create a PR")

    def display(self) -> CellDisplay:
        return CellDisplay(text="+PR", url=self.url)


class PrFetchFailedField(FieldValue):
    """Sentinel placed in the FIELD_PR slot when the PR fetch failed and no
    usable historical PR data is available to fall back to.

    Routes the agent into BoardSection.PRS_FAILED. If a previous cycle
    cached a PrField whose `head_branch` matches the agent's current
    branch, that cached PrField is used instead of emitting this sentinel
    (silent fallback). A cached PrField for a different branch is treated
    as unusable -- the agent has moved on and the old PR would be
    misattributed -- so this sentinel is emitted in that case too.
    """

    kind: Literal["pr_fetch_failed"] = Field(default="pr_fetch_failed", description="Discriminator tag")
    repo: str = Field(description="Repo path that failed to load (e.g. 'org/repo')")

    def display(self) -> CellDisplay:
        return CellDisplay(text="?", color="light red")


class ConflictsField(FieldValue):
    """Merge conflict status for a PR."""

    kind: Literal["conflicts"] = Field(default="conflicts", description="Discriminator tag")
    has_conflicts: bool = Field(description="Whether the PR has merge conflicts")

    def display(self) -> CellDisplay:
        if self.has_conflicts:
            return CellDisplay(text="YES", color="light red")
        return CellDisplay(text="no", color="light green")


_CONFLICTS_ADAPTER: TypeAdapter[FieldValue] = TypeAdapter(ConflictsField)


class UnresolvedField(FieldValue):
    """Unresolved review comment status for a PR."""

    kind: Literal["unresolved"] = Field(default="unresolved", description="Discriminator tag")
    has_unresolved: bool = Field(description="Whether the PR has unresolved review comments")

    def display(self) -> CellDisplay:
        if self.has_unresolved:
            return CellDisplay(text="YES", color="light red")
        return CellDisplay(text="no", color="light green")


_UNRESOLVED_ADAPTER: TypeAdapter[FieldValue] = TypeAdapter(UnresolvedField)


class PrInfo(FrozenModel):
    """PR data assembled from one combined GraphQL query.

    Carries every field the board renders so that `compute()` can construct
    PrField / CiField / ConflictsField / UnresolvedField without any further
    network calls. `has_conflicts` and `has_unresolved` are only consulted
    when `state == OPEN` -- for closed or merged PRs the conflicts and
    unresolved columns are not rendered.
    """

    number: int = Field(description="PR number")
    title: str = Field(description="PR title")
    state: PrState = Field(description="PR state (open/closed/merged)")
    url: str = Field(description="PR URL")
    head_branch: str = Field(description="Head branch name of the PR")
    is_draft: bool = Field(description="Whether the PR is a draft")
    check_status: CiStatus = Field(description="Aggregate CI check status")
    has_conflicts: bool = Field(description="Whether the PR has merge conflicts")
    has_unresolved: bool = Field(description="Whether the PR has unresolved review comments")


class FetchBoardResult(FrozenModel):
    """Result of one `fetch_board` call."""

    prs: dict[tuple[str, str], PrInfo] = Field(description="Mapping from (repo_path, head_branch) to the matching PR.")
    errors: tuple[str, ...] = Field(default=(), description="Per-repo or top-level errors surfaced from gh / GraphQL.")


class _BoardPage(FrozenModel):
    """One page of the cursor-paginated board search response."""

    nodes: tuple[dict[str, Any], ...] = Field(description="Raw PullRequest nodes returned on this page")
    errors: tuple[str, ...] = Field(description="GraphQL error messages surfaced on this page")
    has_next_page: bool = Field(description="Whether GitHub reports a further page after this one")
    end_cursor: str | None = Field(description="Cursor to pass as `after` to fetch the next page, if any")


def fetch_board(
    cg: ConcurrencyGroup,
    repo_branches: Sequence[tuple[str, str]],
    unresolved_ignore_user: str | None = None,
    # The retry policy for a single page request. Injectable so tests can drive
    # the backoff without real sleeps; production callers use the default below.
    page_retrying: Retrying | None = None,
) -> FetchBoardResult:
    """Fetch every PR the kanpan board needs, paging through `gh api graphql`.

    GitHub caps a single search page at 100 results, so when more than 100 PRs
    match (e.g. >100 agents) we follow the `pageInfo` cursor and accumulate
    every page. Pages already fetched are kept even if a later page fails, so a
    failure on page N never re-fetches pages 1..N-1.
    """
    if not repo_branches:
        return FetchBoardResult(prs={})

    retrying = page_retrying if page_retrying is not None else _build_page_retrying()
    all_nodes: list[dict[str, Any]] = []
    errors: list[str] = []
    cursor: str | None = None
    is_page_limit_exceeded = False

    for page_number in range(1, _MAX_SEARCH_PAGES + 1):
        graphql = _build_board_graphql(repo_branches, after=cursor)
        try:
            response = retrying(_run_board_query, cg, graphql)
        except (ProcessError, OSError) as e:
            logger.debug("Failed to launch gh api graphql: {}", e)
            errors.append(f"gh api graphql failed: {e}")
            break
        except GitHubBoardFetchError as e:
            logger.debug("gh api graphql page failed after {} attempts: {}", _PAGE_FETCH_ATTEMPTS, e)
            errors.append(f"gh api graphql page failed after {_PAGE_FETCH_ATTEMPTS} attempts: {e}")
            break

        page = _parse_board_page(response)
        errors.extend(page.errors)
        all_nodes.extend(page.nodes)

        if not page.has_next_page or page.end_cursor is None:
            break
        cursor = page.end_cursor
        if page_number == _MAX_SEARCH_PAGES:
            is_page_limit_exceeded = True

    if is_page_limit_exceeded:
        errors.append(
            f"too many matching PRs: GitHub's search API returned more than "
            f"{_MAX_SEARCH_PAGES * _SEARCH_PAGE_SIZE} results (its hard cap); some agents may render "
            "'Create PR' instead of their merged PR"
        )

    prs = _build_prs_from_nodes(all_nodes, repo_branches, unresolved_ignore_user)
    return FetchBoardResult(prs=prs, errors=tuple(errors))


def _build_page_retrying() -> Retrying:
    """Build the per-page retry policy: bounded attempts with exponential backoff.

    Retries `GitHubBoardFetchError` (a page that returned no search result) and
    re-raises the final failure so the caller can keep the pages fetched so far.
    """
    return Retrying(
        retry=retry_if_exception_type(GitHubBoardFetchError),
        stop=stop_after_attempt(_PAGE_FETCH_ATTEMPTS),
        wait=wait_exponential(
            multiplier=_PAGE_RETRY_BASE_WAIT_SECONDS,
            min=_PAGE_RETRY_MIN_WAIT_SECONDS,
            max=_PAGE_RETRY_MAX_WAIT_SECONDS,
        ),
        reraise=True,
    )


def _run_board_query(cg: ConcurrencyGroup, graphql: str) -> dict[str, Any]:
    """Run one board page query, raising `GitHubBoardFetchError` on no search result.

    Returns the parsed GraphQL response envelope. A successful search always
    carries a `data.s` object (even when it matched nothing); the absence of
    one means the request failed at the transport level -- an unparseable body,
    an HTTP 403 secondary rate limit (whose REST-style error body has no
    `data.s`), a primary `RATE_LIMITED` error (which nulls out `data`), or a
    5xx. The caller retries this same page with backoff; pages already fetched
    are never re-fetched.
    """
    proc = cg.run_process_in_background(
        ["gh", "api", "graphql", "-f", f"query={graphql}"],
        timeout=30,
        is_checked_by_group=False,
    )
    # `gh api graphql` exits non-zero whenever the response carries a GraphQL
    # `errors[]` array, but stdout still holds the full `{data, errors}` JSON;
    # never rely on the exit code alone -- inspect the body instead.
    proc.wait()
    stdout = proc.read_stdout()
    stderr_excerpt = proc.read_stderr().strip()[:_STDERR_EXCERPT_LENGTH]
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Failed to parse gh api graphql output, retrying page: {} (stderr: {})", e, stderr_excerpt)
        raise GitHubBoardFetchError(f"gh api graphql returned no JSON body (stderr: {stderr_excerpt})") from e

    if not isinstance(response, dict) or not isinstance((response.get("data") or {}).get("s"), dict):
        detail = _summarize_failed_response(response, stderr_excerpt)
        logger.warning("gh api graphql returned no search result, retrying page: {}", detail)
        raise GitHubBoardFetchError(f"gh api graphql returned no search result ({detail})")
    return response


@pure
def _summarize_failed_response(response: Any, stderr_excerpt: str) -> str:
    """Build a short, human-readable reason for a page request that returned no search result."""
    if isinstance(response, dict):
        graphql_messages = [
            err["message"] for err in response.get("errors") or () if isinstance(err, dict) and "message" in err
        ]
        if graphql_messages:
            return "; ".join(graphql_messages)
        top_level_message = response.get("message")
        if isinstance(top_level_message, str) and top_level_message:
            return top_level_message
    return f"stderr: {stderr_excerpt}" if stderr_excerpt else "no data returned"


def _build_board_graphql(repo_branches: Sequence[tuple[str, str]], after: str | None = None) -> str:
    """Build the GraphQL document that fetches one page of every requested (repo, branch).

    GitHub's search query syntax treats multiple `repo:` and `head:`
    qualifiers within a single `search()` call as OR, which is the
    SQL `WHERE repo IN (...) AND head IN (...)` equivalent. That lets a
    single `search()` cover every (repo, branch) pair without aliasing
    one subquery per pair.

    `first: 100` is GitHub's hard per-page cap. When the matched set exceeds
    one page, the caller follows `pageInfo.endCursor` and re-invokes this with
    `after` set to fetch subsequent pages.
    """
    repos = sorted({rb[0] for rb in repo_branches})
    branches = sorted({rb[1] for rb in repo_branches})
    repo_clause = " OR ".join(f"repo:{r}" for r in repos)
    branch_clause = " OR ".join(f"head:{b}" for b in branches)
    search_query = f"type:pr author:@me ({repo_clause}) ({branch_clause})"
    # json.dumps gives a properly escaped, double-quoted GraphQL string literal.
    quoted_search = json.dumps(search_query)
    after_clause = f", after: {json.dumps(after)}" if after is not None else ""

    return f"""query KanpanBoard {{
  s: search(query: {quoted_search}, type: ISSUE_ADVANCED, first: {_SEARCH_PAGE_SIZE}{after_clause}) {{
    nodes {{
      ... on PullRequest {{
        number
        title
        url
        headRefName
        isDraft
        state
        mergeable
        statusCheckRollup {{ state }}
        reviewThreads(first: {_SEARCH_PAGE_SIZE}) {{
          nodes {{
            isResolved
            comments(last: 1) {{ nodes {{ author {{ login }} }} }}
          }}
        }}
        comments(last: 1) {{ nodes {{ author {{ login }} }} }}
        repository {{ nameWithOwner }}
      }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""


def _parse_board_page(response: dict[str, Any]) -> _BoardPage:
    """Extract one page's nodes, cursor, and any partial errors from a parsed response.

    Robust to partial GraphQL errors (data + errors both present) and null
    nodes (returned for non-PullRequest matches in ISSUE_ADVANCED searches).
    The response is guaranteed to carry a `data.s` object by the caller.
    """
    errors: list[str] = []
    for err in response.get("errors") or ():
        message = err.get("message", "unknown GraphQL error") if isinstance(err, dict) else str(err)
        errors.append(message)

    search_result = (response.get("data") or {}).get("s") or {}
    # Drop null nodes here so downstream consumers only ever see dict nodes.
    nodes = tuple(node for node in (search_result.get("nodes") or []) if isinstance(node, dict))

    page_info = search_result.get("pageInfo") or {}
    end_cursor = page_info.get("endCursor")
    return _BoardPage(
        nodes=nodes,
        errors=tuple(errors),
        has_next_page=bool(page_info.get("hasNextPage")),
        end_cursor=end_cursor if isinstance(end_cursor, str) else None,
    )


def _build_prs_from_nodes(
    nodes: Sequence[dict[str, Any]],
    repo_branches: Sequence[tuple[str, str]],
    unresolved_ignore_user: str | None,
) -> dict[tuple[str, str], PrInfo]:
    """Reduce the accumulated PullRequest nodes to one PrInfo per requested (repo, branch)."""
    requested = set(repo_branches)
    by_key: dict[tuple[str, str], list[PrInfo]] = {}
    for node in nodes:
        repo = (node.get("repository") or {}).get("nameWithOwner")
        branch = node.get("headRefName")
        if not isinstance(repo, str) or not isinstance(branch, str):
            continue
        key = (repo, branch)
        if key not in requested:
            # Defensive: should be impossible given our query, but skip rather than misattribute.
            continue
        by_key.setdefault(key, []).append(_parse_pr_node(node, unresolved_ignore_user))

    # If multiple PRs share the same head branch (e.g. closed-then-reopened),
    # prefer OPEN > MERGED > CLOSED to match what kanpan used to do via
    # `_build_pr_branch_index`.
    return {key: max(cands, key=_pr_state_rank) for key, cands in by_key.items()}


def _parse_pr_node(node: dict[str, Any], unresolved_ignore_user: str | None) -> PrInfo:
    """Parse one PullRequest node from the combined search response."""
    return PrInfo(
        number=node["number"],
        title=node["title"],
        state=_parse_pr_state(node["state"]),
        url=node["url"],
        head_branch=node["headRefName"],
        is_draft=bool(node.get("isDraft", False)),
        check_status=CiStatus.from_rollup_state((node.get("statusCheckRollup") or {}).get("state")),
        has_conflicts=node.get("mergeable") == "CONFLICTING",
        has_unresolved=_check_unresolved_threads(node, unresolved_ignore_user),
    )


@pure
def _parse_pr_state(state_str: str) -> PrState:
    """Convert the GraphQL `PullRequestState` enum to `PrState`."""
    upper = state_str.upper()
    if upper == "MERGED":
        return PrState.MERGED
    if upper == "CLOSED":
        return PrState.CLOSED
    return PrState.OPEN


def _check_unresolved_threads(node: dict[str, Any], ignore_user: str | None) -> bool:
    """Determine whether a PR has unresolved review threads or unanswered PR comments.

    Inline review threads: return True if any thread has `isResolved=False`
    (and, if `ignore_user` is set, the last comment is not by that user --
    if the last reply was yours, the ball is in their court so we skip it).

    PR conversation: when `ignore_user` is set, also return True if the
    last conversation comment is by someone other than `ignore_user`.
    Without `ignore_user`, PR conversation comments don't gate the column.

    Reads directly off the PullRequest node returned by the combined search
    query (no separate JSON to parse, no separate GraphQL request).
    """
    threads = (node.get("reviewThreads") or {}).get("nodes") or []
    for thread in threads:
        # `reviewThreads.nodes` is `[PullRequestReviewThread]` -- the inner type
        # is nullable per the GraphQL schema, so an individual entry can come
        # back as `null` if e.g. one specific thread was deleted between
        # connection-counting and field resolution. Skip the position rather
        # than blowing up the whole refresh.
        if thread is None or thread.get("isResolved", True):
            continue
        if ignore_user is not None:
            comments = (thread.get("comments") or {}).get("nodes") or []
            if comments:
                author = (comments[0].get("author") or {}).get("login")
                if author == ignore_user:
                    continue
        return True

    pr_comments = (node.get("comments") or {}).get("nodes") or []
    if pr_comments and ignore_user is not None:
        last_author = (pr_comments[0].get("author") or {}).get("login")
        if last_author is not None and last_author != ignore_user:
            return True
    return False


@pure
def _pr_state_rank(pr: PrInfo) -> int:
    """Priority for picking among multiple PRs that share a head branch."""
    if pr.state == PrState.OPEN:
        return 2
    if pr.state == PrState.MERGED:
        return 1
    return 0


# Discriminated-union adapter for the FIELD_PR slot. The slot is polymorphic --
# a real PR is a PrField, a pushed-but-no-PR branch is a CreatePrUrlField, and
# a fetch failure with no cached fallback is a PrFetchFailedField. The
# `kind` Literal on each subclass is the discriminator, so pydantic picks the
# right concrete class without order-sensitive trial validation.
PrSlotField = Annotated[
    PrField | CreatePrUrlField | PrFetchFailedField,
    Field(discriminator="kind"),
]
_PR_SLOT_ADAPTER: TypeAdapter[FieldValue] = TypeAdapter(PrSlotField)


class GitHubDataSourceConfig(DataSourceConfig):
    """Configuration for the GitHub data source."""

    pr: bool = Field(default=True, description="Fetch PR number/URL/state/draft")
    ci: bool = Field(default=True, description="Fetch CI check status")
    conflicts: bool = Field(default=True, description="Check merge conflict status")
    unresolved: bool = Field(default=True, description="Check unresolved PR comments")
    unresolved_ignore_user: str | None = Field(
        default=None,
        description="GitHub username whose review threads to ignore when checking for unresolved comments. "
        "Threads where the last comment is by this user are skipped (you already replied).",
    )


class GitHubDataSource(FrozenModel):
    """Fetches GitHub PR, CI, conflict, and unresolved comment data.

    Uses the GitHub GraphQL API via `gh api graphql`. Reads `repo_path`
    from cached fields (produced by `RepoPathsDataSource` in the previous
    cycle) and from agent labels.

    All data for the entire board is fetched in a single GraphQL request
    via `fetch_board` -- there is no per-repo or per-PR fan-out.
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
        if self.config.conflicts:
            cols[FIELD_CONFLICTS] = "CONFLICTS"
        if self.config.unresolved:
            cols[FIELD_UNRESOLVED] = "UNRESOLVED"
        return cols

    @property
    def field_types(self) -> dict[str, TypeAdapter[FieldValue]]:
        types: dict[str, TypeAdapter[FieldValue]] = {}
        if self.config.pr:
            types[FIELD_PR] = _PR_SLOT_ADAPTER
        if self.config.ci:
            types[FIELD_CI] = _CI_ADAPTER
        if self.config.conflicts:
            types[FIELD_CONFLICTS] = _CONFLICTS_ADAPTER
        if self.config.unresolved:
            types[FIELD_UNRESOLVED] = _UNRESOLVED_ADAPTER
        return types

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        cg = mngr_ctx.concurrency_group
        errors: list[str] = []
        now = now_utc()

        # Resolve repo_path per agent. Labels are world data (refreshed every
        # list_agents call) so they are always at least as fresh as the cached
        # RepoPathField; prefer labels for both value and freshness. Fall back
        # to the cache only when labels no longer carry a remote -- in which
        # case the cached value is the only information we have and its
        # `created` correctly tags any derived field as stale.
        agent_repos: dict[AgentName, str] = {}
        agent_created: dict[AgentName, datetime] = {}
        for agent in agents:
            label_repo = repo_path_from_labels(agent.labels)
            if label_repo is not None:
                agent_repos[agent.name] = label_repo
                agent_created[agent.name] = now
                continue
            cached_repo_field = _get_cached_repo_field(cached_fields, agent.name)
            if cached_repo_field is not None:
                agent_repos[agent.name] = cached_repo_field.path
                agent_created[agent.name] = cached_repo_field.created

        # Collect every (repo, branch) pair we need to look up.
        repo_branches: list[tuple[str, str]] = []
        for agent in agents:
            agent_repo = agent_repos.get(agent.name)
            branch = agent.initial_branch
            if agent_repo is not None and branch is not None:
                repo_branches.append((agent_repo, branch))

        if not repo_branches:
            return {}, errors

        board = fetch_board(
            cg,
            repo_branches,
            unresolved_ignore_user=self.config.unresolved_ignore_user,
        )
        errors.extend(board.errors)
        fetch_failed = bool(board.errors)

        fields: dict[AgentName, dict[str, FieldValue]] = {}
        for agent in agents:
            agent_repo = agent_repos.get(agent.name)
            branch = agent.initial_branch
            if agent_repo is None or branch is None:
                continue

            # `this_created` is the staleness time for any field we synthesize
            # for this agent: it reflects how stale the (repo, branch) lookup
            # is (now if from labels, cached.created if we fell back to the
            # cache). The PR data itself was fetched just now, but its
            # attribution to this agent rides on a possibly-stale mapping.
            this_created = agent_created[agent.name]
            pr_info = board.prs.get((agent_repo, branch))

            if pr_info is not None:
                agent_fields = _compute_pr_fields(pr_info, this_created, self.config)
            elif not fetch_failed:
                # Fetch succeeded; branch genuinely has no PR yet.
                agent_fields = _compute_no_pr_fields(agent_repo, branch, this_created, self.config)
            else:
                # Fetch errored. Use a cached PrField if one matches this branch,
                # otherwise show a PrFetchFailedField sentinel.
                agent_fields = _compute_failed_fetch_fields(
                    cached_fields, agent.name, branch, agent_repo, this_created, self.config
                )

            if agent_fields:
                fields[agent.name] = agent_fields

        return fields, errors


def _get_cached_repo_field(
    cached_fields: dict[AgentName, dict[str, FieldValue]], agent_name: AgentName
) -> RepoPathField | None:
    """Get the cached RepoPathField (with its `created` timestamp) if available."""
    agent_cached = cached_fields.get(agent_name)
    if agent_cached is None:
        return None
    repo_field = agent_cached.get(FIELD_REPO_PATH)
    if isinstance(repo_field, RepoPathField):
        return repo_field
    return None


@pure
def _build_create_pr_url(repo_path: str, branch: str) -> str:
    """Build a GitHub URL for creating a new PR from the given branch."""
    return f"https://github.com/{repo_path}/compare/{branch}?expand=1"


@pure
def _compute_no_pr_fields(
    agent_repo: str,
    branch: str,
    this_created: datetime,
    config: GitHubDataSourceConfig,
) -> dict[str, FieldValue]:
    """Build the per-agent fields when the fetch succeeded but the agent's
    branch has no PR yet -- emit a CreatePrUrlField so the agent renders a
    'Create PR' link in the FIELD_PR slot.
    """
    if not config.pr:
        return {}
    return {FIELD_PR: CreatePrUrlField(url=_build_create_pr_url(agent_repo, branch), created=this_created)}


@pure
def _compute_pr_fields(
    pr_info: PrInfo,
    this_created: datetime,
    config: GitHubDataSourceConfig,
) -> dict[str, FieldValue]:
    """Build the per-agent FIELD_PR / FIELD_CI / FIELD_CONFLICTS / FIELD_UNRESOLVED
    fields when the agent's (repo, branch) lookup resolved to a real PR.

    Conflicts and unresolved are only emitted for OPEN PRs -- closed and merged
    PRs don't expose a meaningful conflict-or-unresolved status to act on.
    """
    agent_fields: dict[str, FieldValue] = {}
    if config.pr:
        agent_fields[FIELD_PR] = PrField(
            number=pr_info.number,
            url=pr_info.url,
            is_draft=pr_info.is_draft,
            title=pr_info.title,
            state=pr_info.state,
            head_branch=pr_info.head_branch,
            created=this_created,
        )
    if config.ci:
        agent_fields[FIELD_CI] = CiField(status=pr_info.check_status, created=this_created)
    if pr_info.state == PrState.OPEN:
        if config.conflicts:
            agent_fields[FIELD_CONFLICTS] = ConflictsField(has_conflicts=pr_info.has_conflicts, created=this_created)
        if config.unresolved:
            agent_fields[FIELD_UNRESOLVED] = UnresolvedField(
                has_unresolved=pr_info.has_unresolved, created=this_created
            )
    return agent_fields


@pure
def _compute_failed_fetch_fields(
    cached_fields: dict[AgentName, dict[str, FieldValue]],
    agent_name: AgentName,
    branch: str,
    agent_repo: str,
    this_created: datetime,
    config: GitHubDataSourceConfig,
) -> dict[str, FieldValue]:
    """Build the FIELD_PR / FIELD_CI fields for an agent whose PR fetch failed.

    Silently falls back to a cached PrField/CiField if available; otherwise
    emits a PrFetchFailedField so the agent shows up under "PRs not loaded"
    instead of being misclassified as "no PR yet".

    Branch match: only reuse the cache when the cached PR's head_branch
    equals the agent's current branch. Otherwise the agent has moved on to
    a different branch since the cache was written, and showing the old
    PR would misattribute it to the wrong branch.

    Staleness: there is no TTL on the cached PR. If the fetch keeps failing
    for hours, we keep showing the last-known PR row (number, state, CI).
    The cached fields carry their own `created` so the TUI renders them
    as stale once they age past the staleness threshold. The
    PrFetchFailedField is stamped with `this_created` (the lookup
    freshness for this agent) for the same taint-propagation reason as
    the success path.
    """
    agent_fields: dict[str, FieldValue] = {}
    cached_agent = cached_fields.get(agent_name, {})
    cached_pr = cached_agent.get(FIELD_PR)
    if isinstance(cached_pr, PrField) and cached_pr.head_branch == branch:
        if config.pr:
            agent_fields[FIELD_PR] = cached_pr
        if config.ci:
            cached_ci = cached_agent.get(FIELD_CI)
            if isinstance(cached_ci, CiField):
                agent_fields[FIELD_CI] = cached_ci
    elif config.pr:
        agent_fields[FIELD_PR] = PrFetchFailedField(repo=agent_repo, created=this_created)
    return agent_fields
