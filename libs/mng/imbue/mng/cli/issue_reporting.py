import json
import sys
import traceback
import webbrowser
from typing import Final
from typing import NoReturn
from urllib.parse import quote
from urllib.parse import urlencode

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mng.errors import BaseMngError

GITHUB_REPO: Final[str] = "imbue-ai/mng"
GITHUB_BASE_URL: Final[str] = f"https://github.com/{GITHUB_REPO}"
ISSUE_TITLE_PREFIX: Final[str] = "[NotImplemented]"
UNEXPECTED_ERROR_TITLE_PREFIX: Final[str] = "[Bug]"

# Maximum URL length to stay within browser and GitHub limits
_MAX_URL_LENGTH: Final[int] = 8000

_TRUNCATION_SUFFIX: Final[str] = "\n\n_(truncated)_"


class IssueSearchError(BaseMngError):
    """Raised when searching for GitHub issues fails."""


class ExistingIssue(FrozenModel):
    """A GitHub issue that already exists matching an error report."""

    number: int = Field(description="GitHub issue number")
    title: str = Field(description="GitHub issue title")
    url: str = Field(description="URL to the GitHub issue")


@pure
def build_issue_title(error_message: str) -> str:
    """Build a GitHub issue title from a NotImplementedError message."""
    first_line = error_message.strip().split("\n")[0]
    return f"{ISSUE_TITLE_PREFIX} {first_line}"


@pure
def build_issue_body(error_message: str) -> str:
    """Build a GitHub issue body from a NotImplementedError message."""
    return (
        "## Feature Request\n"
        "\n"
        "This feature is referenced in the code but not yet implemented.\n"
        "\n"
        "**Error message:**\n"
        f"```\n{error_message}\n```\n"
        "\n"
        "## Use Case\n"
        "\n"
        "_Please describe your use case here._\n"
    )


@pure
def _make_issue_url(title: str, body: str) -> str:
    """Build a full GitHub new-issue URL from title and body."""
    params = urlencode({"title": title, "body": body}, quote_via=quote)
    return f"{GITHUB_BASE_URL}/issues/new?{params}"


@pure
def build_new_issue_url(title: str, body: str) -> str:
    """Build a GitHub URL for creating a new issue with pre-populated fields."""
    full_url = _make_issue_url(title, body)

    # Truncate body if URL exceeds max length
    if len(full_url) > _MAX_URL_LENGTH:
        # Over-estimate how much to trim (URL encoding can expand characters)
        overage = len(full_url) - _MAX_URL_LENGTH
        truncated_body = body[: len(body) - overage - len(_TRUNCATION_SUFFIX) - 50] + _TRUNCATION_SUFFIX
        full_url = _make_issue_url(title, truncated_body)

    return full_url


def _search_issues_via_github_api(search_text: str, cg: ConcurrencyGroup) -> ExistingIssue | None:
    """Search for existing issues using the GitHub REST API via curl."""
    query = f"{search_text} repo:{GITHUB_REPO} is:issue"
    url = f"https://api.github.com/search/issues?q={quote(query)}&per_page=1"

    try:
        result = cg.run_process_to_completion(
            ["curl", "-s", "-f", "-H", "Accept: application/vnd.github+json", url],
            timeout=10,
        )
    except ConcurrencyGroupError as e:
        raise IssueSearchError(f"GitHub API request failed: {e}") from e

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise IssueSearchError(f"Failed to parse GitHub API response: {e}") from e

    items = data.get("items", [])

    if not items:
        return None

    item = items[0]
    return ExistingIssue(
        number=item["number"],
        title=item["title"],
        url=item["html_url"],
    )


def _search_issues_via_gh_cli(search_text: str, cg: ConcurrencyGroup) -> ExistingIssue | None:
    """Search for existing issues using the gh CLI (works for private repos)."""
    try:
        result = cg.run_process_to_completion(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                GITHUB_REPO,
                "--search",
                search_text,
                "--json",
                "number,title,url",
                "--limit",
                "1",
            ],
            timeout=10,
        )
    except ConcurrencyGroupError as e:
        raise IssueSearchError(f"gh CLI search failed: {e}") from e

    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise IssueSearchError(f"Failed to parse gh CLI response: {e}") from e

    if not items:
        return None

    item = items[0]
    return ExistingIssue(
        number=item["number"],
        title=item["title"],
        url=item["url"],
    )


def search_for_existing_issue(search_text: str, cg: ConcurrencyGroup) -> ExistingIssue | None:
    """Search for an existing GitHub issue matching the error message."""
    try:
        return _search_issues_via_github_api(search_text, cg)
    except IssueSearchError:
        logger.debug("GitHub API search failed, falling back to gh CLI")

    try:
        return _search_issues_via_gh_cli(search_text, cg)
    except IssueSearchError:
        logger.debug("gh CLI search also failed")

    return None


def _format_existing_issue_message(issue: ExistingIssue) -> str:
    return "Found existing issue " + str(issue.number) + ": " + issue.title


def _prompt_and_report_issue(title: str, body: str, search_text: str) -> None:
    """Prompt the user to report a GitHub issue and open the browser.

    Searches for an existing issue matching search_text. If found, opens that
    issue's URL. Otherwise, opens the new issue form pre-populated with title
    and body.
    """
    if not click.confirm("\nWould you like to report this as a GitHub issue?", default=True):
        return

    # Search for existing issue using a standalone ConcurrencyGroup
    logger.info("Searching for existing issues...")
    with ConcurrencyGroup(name="issue-search") as cg:
        existing = search_for_existing_issue(search_text, cg)

    if existing is not None:
        logger.info("{}", _format_existing_issue_message(existing))
        logger.info("Opening: {}", existing.url)
        webbrowser.open(existing.url)
    else:
        logger.info("No existing issue found. Opening new issue form...")
        url = build_new_issue_url(title, body)
        webbrowser.open(url)


def handle_not_implemented_error(error: NotImplementedError, is_interactive: bool | None = None) -> NoReturn:
    """Handle a NotImplementedError by showing the error and optionally reporting it."""
    error_message = str(error) if str(error) else "Feature not implemented"

    # Always show the error message
    logger.error("Error: {}", error_message)

    # Resolve interactivity: explicit parameter takes priority, then fall back to TTY check
    is_interactive_resolved = is_interactive if is_interactive is not None else sys.stdin.isatty()

    # In non-interactive mode, just exit
    if not is_interactive_resolved:
        raise SystemExit(1)

    # In interactive mode, offer to report
    title = build_issue_title(error_message)
    body = build_issue_body(error_message)
    _prompt_and_report_issue(title, body, error_message)

    raise SystemExit(1)


# FIXME: actually, to make this sane, we want to search just for the type of error being raised, and the function it is being raised from (the lowest level one in the traceback that is actually from one of our libraries)
#  otherwise we're likely to end up missing existing issues, esp if there is anything random or dynamic in the error message (e.g. memory addresses, random IDs, etc.) that would prevent matching against existing issues.
@pure
def build_unexpected_error_issue_title(error: Exception) -> str:
    """Build a GitHub issue title from an unexpected error."""
    error_type = type(error).__name__
    error_message = str(error).strip().split("\n")[0] if str(error) else "No message"
    return f"{UNEXPECTED_ERROR_TITLE_PREFIX} {error_type}: {error_message}"


@pure
def build_unexpected_error_issue_body(error: Exception, traceback_str: str) -> str:
    """Build a GitHub issue body from an unexpected error with traceback."""
    return (
        "## Bug Report\n"
        "\n"
        "An unexpected error occurred during command execution.\n"
        "\n"
        "**Error:**\n"
        f"```\n{type(error).__name__}: {error}\n```\n"
        "\n"
        "**Traceback:**\n"
        f"```\n{traceback_str}\n```\n"
        "\n"
        "## Additional Context\n"
        "\n"
        "_Please describe what you were doing when this error occurred._\n"
    )


def handle_unexpected_error(error: Exception, is_interactive: bool | None = None) -> NoReturn:
    """Handle an unexpected error by showing the traceback and optionally reporting it."""
    tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    # Show the full traceback
    logger.error("Unexpected error:\n{}", tb_str)

    # Resolve interactivity: explicit parameter takes priority, then fall back to TTY check
    is_interactive_resolved = is_interactive if is_interactive is not None else sys.stdin.isatty()

    # In non-interactive mode, just exit
    if not is_interactive_resolved:
        raise SystemExit(1)

    # In interactive mode, offer to report
    error_message = str(error) if str(error) else type(error).__name__
    title = build_unexpected_error_issue_title(error)
    body = build_unexpected_error_issue_body(error, tb_str)
    _prompt_and_report_issue(title, body, error_message)

    raise SystemExit(1)
