import hashlib
import importlib.metadata
import json
import os
import sys
import traceback
import webbrowser
from pathlib import Path
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
from imbue.mngr.errors import BaseMngrError

GITHUB_REPO: Final[str] = "imbue-ai/mngr"
GITHUB_BASE_URL: Final[str] = f"https://github.com/{GITHUB_REPO}"
ISSUE_TITLE_PREFIX: Final[str] = "[NotImplemented]"
# Maximum URL length to stay within browser and GitHub limits
_MAX_URL_LENGTH: Final[int] = 8000

_TRUNCATION_SUFFIX: Final[str] = "\n\n_(truncated)_"


class IssueSearchError(BaseMngrError):
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
    # don't bother reporting when this is autonomous
    if os.environ.get("IS_AUTONOMOUS", "0") == "1":
        return

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


def get_mngr_version() -> str:
    """Get the installed mngr version, falling back to 'unknown'."""
    try:
        return importlib.metadata.version("imbue-mngr")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


MNGR_REPO_URL: Final[str] = f"https://github.com/{GITHUB_REPO}.git"


@pure
def build_diagnose_prompt(
    error_type: str,
    error_message: str,
    traceback_str: str,
    mngr_version: str,
) -> str:
    """Build the initial message for the diagnostic agent.

    The agent receives this as its starting prompt, running inside a worktree
    of the mngr repo that was cloned from MNGR_REPO_URL.
    """
    parts: list[str] = [
        "You are diagnosing a bug in the `mngr` CLI tool (https://github.com/imbue-ai/mngr).",
        "You are working inside a worktree of the repository.",
        "",
        "## Task",
        "Find the root cause of this bug and prepare a GitHub issue report.",
        "",
        "Your report should include:",
        "- Root cause analysis with specific file/line references",
        "- Minimal reproduction steps or the error traceback (whichever better demonstrates the bug)",
        "- If helpful, edit the code to test your hypothesis about the cause -- you can",
        "  include a git diff in the issue as evidence that you've verified the root cause",
        "",
        "The issue body must include an **Environment** section with:",
        f"- mngr version: {mngr_version}",
        "- Commit hash inspected: run `git rev-parse HEAD` in this worktree",
        "- Versions of any other tools relevant to the issue",
        "",
        "Write your issue body to a markdown file, then run:",
        '  python scripts/open_issue.py --title "Your issue title" body.md',
        "This will open the issue in the browser for the user to review before submission.",
        "",
        "## Problem Description",
        f"{error_type}: {error_message}",
        "",
        "## Error Traceback",
        "```",
        traceback_str,
        "```",
    ]
    return "\n".join(parts)


def write_diagnose_prompt_file(
    traceback_str: str,
    mngr_version: str,
    error_type: str,
    error_message: str,
) -> Path:
    """Write a diagnostic-agent prompt to a temp file for `mngr create --message-file`.

    Returns the path to the written file. Content-addressed so repeated calls
    with the same inputs produce the same path.
    """
    prompt = build_diagnose_prompt(
        error_type=error_type,
        error_message=error_message,
        traceback_str=traceback_str,
        mngr_version=mngr_version,
    )
    content_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    path = Path(f"/tmp/mngr-diagnose-prompt-{content_hash}.txt")
    path.write_text(prompt)
    return path


def _offer_diagnose(prompt_file_path: Path) -> None:
    """Print a `mngr create` command the user can run to launch a diagnostic agent."""
    if os.environ.get("IS_AUTONOMOUS", "0") == "1":
        return

    create_cmd = (
        f"mngr create --source {MNGR_REPO_URL} --branch main: --message-file {prompt_file_path}"
    )

    logger.info("")
    logger.info("To launch an agent to diagnose this problem, run:")
    logger.info("  {}", create_cmd)


def handle_unexpected_error(error: Exception, is_interactive: bool | None = None) -> NoReturn:
    """Handle an unexpected error by logging the traceback and suggesting a diagnosis.

    Writes the error traceback + a diagnostic prompt to a temp file, then prints
    a copy-paste-ready `mngr create` command that launches an agent in a worktree
    of the mngr repo with that prompt as its initial message.
    """
    tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    # Show the full traceback
    logger.error("Unexpected error:\n{}", tb_str)

    # Resolve interactivity: explicit parameter takes priority, then fall back to TTY check
    is_interactive_resolved = is_interactive if is_interactive is not None else sys.stdin.isatty()

    # In non-interactive mode, just exit
    if not is_interactive_resolved:
        raise SystemExit(1)

    error_message = str(error) if str(error) else type(error).__name__

    # Write the prompt file and print a `mngr create` command the user can copy-paste.
    # If writing the prompt raises (e.g. disk full), let it propagate -- the original
    # error has already been logged above.
    prompt_path = write_diagnose_prompt_file(
        traceback_str=tb_str,
        mngr_version=get_mngr_version(),
        error_type=type(error).__name__,
        error_message=error_message,
    )
    _offer_diagnose(prompt_path)

    raise SystemExit(1)
