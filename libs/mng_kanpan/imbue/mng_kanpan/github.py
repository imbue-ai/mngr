import json
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.pure import pure
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState


class FetchPrsResult:
    """Result of fetching PRs from GitHub."""

    def __init__(self, prs: tuple[PrInfo, ...], error: str | None) -> None:
        self.prs = prs
        self.error = error


def fetch_all_prs(cg: ConcurrencyGroup, cwd: Path | None = None) -> FetchPrsResult:
    """Fetch all PRs from the current repo using gh CLI.

    Runs gh pr list to get recent PRs in all states. The cwd parameter should
    point to a directory inside the target git repository so that gh can detect
    which GitHub repo to query.

    Returns a FetchPrsResult with the PRs and any error message.
    """
    try:
        result = cg.run_process_to_completion(
            [
                "gh",
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "all",
                "--json",
                "number,title,state,headRefName,url,statusCheckRollup,isDraft",
                "--limit",
                "500",
            ],
            timeout=30,
            cwd=cwd,
        )
        raw_prs: list[dict[str, Any]] = json.loads(result.stdout)
        return FetchPrsResult(prs=tuple(_parse_pr(raw) for raw in raw_prs), error=None)
    except ProcessError as e:
        logger.debug("Failed to fetch PRs from GitHub: {}", e)
        return FetchPrsResult(prs=(), error=f"gh pr list failed: {e.stderr.strip() or e.stdout.strip() or str(e)}")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug("Failed to parse PR data from GitHub: {}", e)
        return FetchPrsResult(prs=(), error=f"Failed to parse gh output: {e}")


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
