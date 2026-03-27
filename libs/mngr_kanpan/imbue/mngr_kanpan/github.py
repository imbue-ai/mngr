import json
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_kanpan.data_types import CheckStatus
from imbue.mngr_kanpan.data_types import PrInfo
from imbue.mngr_kanpan.data_types import PrState

_BASE_FIELDS = "number,title,state,headRefName,url,isDraft"
_OPEN_FIELDS = f"{_BASE_FIELDS},statusCheckRollup"


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
def _parse_check_status(rollup: list[dict[str, Any]] | None) -> CheckStatus:
    """Derive aggregate check status from statusCheckRollup.

    Priority: any failure -> FAILING, any pending -> PENDING,
    all success -> PASSING, empty/None -> UNKNOWN.
    """
    if not rollup:
        return CheckStatus.UNKNOWN

    has_pending = False
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()

        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            return CheckStatus.FAILING
        if status != "COMPLETED":
            has_pending = True

    if has_pending:
        return CheckStatus.PENDING
    return CheckStatus.PASSING
