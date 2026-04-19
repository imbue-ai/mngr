from imbue.imbue_common.pure import pure


@pure
def build_diagnose_initial_message(
    description: str | None,
    traceback_str: str | None,
    mngr_version: str,
) -> str:
    """Build the initial message for the diagnostic agent."""
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
        "If the information provided is not detailed enough for you to know where to start",
        "diagnosing, ask the user for more details before proceeding.",
        "",
        "Write your issue body to a markdown file, then run:",
        '  python scripts/open-issue.py --title "Your issue title" body.md',
        "This will open the issue in the browser for the user to review before submission.",
        "",
        "## mngr Version",
        mngr_version,
    ]

    if description is not None:
        parts.extend(["", "## Problem Description", description])

    if traceback_str is not None:
        parts.extend(["", "## Error Traceback", "```", traceback_str, "```"])

    if description is None and traceback_str is None:
        parts.extend(["", "No details were provided. Ask the user to describe the problem before proceeding."])

    return "\n".join(parts)
