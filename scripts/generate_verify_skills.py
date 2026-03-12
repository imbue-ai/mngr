"""Generate and assemble verify skill markdown files.

Two modes:

  assemble    Combine local source files into final skill files (no external deps).
              Sources: scripts/verify_skill_sources/{verify-and-fix-preamble,
                       branch-categories,conversation-categories}.md
              Output:  .claude/skills/autofix/verify-and-fix.md
                       .claude/skills/verify-conversation/categories.md

  from-vet    Regenerate vet-sourced intermediate files from a checkout of imbue-ai/vet.
              Requires --vet-repo or VET_REPO env var.
              Output:  scripts/verify_skill_sources/{branch-categories,
                       conversation-categories}.md

Both modes support --check to verify on-disk files are up to date without writing.

Usage:
    uv run python scripts/generate_verify_skills.py assemble
    uv run python scripts/generate_verify_skills.py assemble --check
    uv run python scripts/generate_verify_skills.py from-vet --vet-repo /path/to/vet
    VET_REPO=/path/to/vet uv run python scripts/generate_verify_skills.py from-vet --check
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Source files (checked in, hand-edited or vet-generated)
SOURCES_DIR = SCRIPT_DIR / "verify_skill_sources"
PREAMBLE_PATH = SOURCES_DIR / "verify-and-fix-preamble.md"
BRANCH_CATEGORIES_PATH = SOURCES_DIR / "branch-categories.md"
CONVERSATION_CATEGORIES_PATH = SOURCES_DIR / "conversation-categories.md"

# Final skill files (assembled from source files above)
VERIFY_AND_FIX_PATH = REPO_ROOT / ".claude" / "skills" / "autofix" / "verify-and-fix.md"
CONVERSATION_CATEGORIES_SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "verify-conversation" / "categories.md"


# ---------------------------------------------------------------------------
# Assemble mode: combine local files into final skill files
# ---------------------------------------------------------------------------


def assemble_verify_and_fix() -> str:
    """Assemble verify-and-fix.md from preamble + branch categories."""
    preamble = PREAMBLE_PATH.read_text()
    categories = BRANCH_CATEGORIES_PATH.read_text()
    return preamble.rstrip() + "\n\n---\n\n" + categories


def assemble_conversation_categories() -> str:
    """Assemble conversation categories (direct copy from source)."""
    return CONVERSATION_CATEGORIES_PATH.read_text()


ASSEMBLE_TARGETS: dict[str, tuple[Path, callable]] = {
    "verify-and-fix": (VERIFY_AND_FIX_PATH, assemble_verify_and_fix),
    "conversation-categories": (CONVERSATION_CATEGORIES_SKILL_PATH, assemble_conversation_categories),
}


# ---------------------------------------------------------------------------
# From-vet mode: regenerate vet-sourced files
# ---------------------------------------------------------------------------


def format_guide_section(guide) -> str:
    """Format a single IssueIdentificationGuide into a markdown section."""
    lines: list[str] = []

    lines.append(f"## {guide.issue_code.value}")
    lines.append("")

    lines.append(guide.guide)
    lines.append("")

    if guide.examples:
        lines.append("**Examples:**")
        for example in guide.examples:
            lines.append(f"- {example}")
        lines.append("")

    if guide.exceptions:
        lines.append("**Exceptions:**")
        for exception in guide.exceptions:
            lines.append(f"- {exception}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


BRANCH_PREAMBLE = textwrap.dedent("""\
    # Issue Categories

    Review the code for the following types of issues:
""")

CONVERSATION_PREAMBLE = textwrap.dedent("""\
    # Issue Categories

    Review the conversation for the following types of issues:
""")

CONVERSATION_OUTPUT_FORMAT = textwrap.dedent("""\
    ## Output Format

    After your analysis when you are creating the final json file of issues, make a JSON record with each of the following fields (in order) for each issue you decide is valid to report, and append it as a new line to the final output json file:

    - issue_type: the issue type code from above (e.g., "misleading_behavior", "instruction_file_disobeyed", "instruction_to_save")
    - description: a complete description of the issue. Phrase it collaboratively rather than combatively -- the response will be given as feedback to the agent
    - confidence_reasoning: the thought process for how confident you are that it is an issue at all
    - confidence: a confidence score between 0.0 and 1.0 (1.0 = absolutely certain it is an issue, 0.0 = no confidence at all, should roughly be the probability that it is an actual issue to 1 decimal place)
    - severity_reasoning: the thought process for how severe the issue is (assuming it were an issue, i.e., ignoring confidence)
    - severity: one of "CRITICAL", "MAJOR", "MINOR", or "NITPICK", where
        - CRITICAL: must be addressed; the agent fundamentally failed to do what was asked or made a serious error
        - MAJOR: should be addressed; the agent missed something significant or made a meaningful mistake
        - MINOR: could be addressed; the agent's work has a minor gap or issue
        - NITPICK: optional; a very minor observation
""")


def generate_branch_categories(vet_modules) -> str:
    """Generate branch issue categories from vet: batched commit + correctness guides."""
    codes_batch = vet_modules["ISSUE_CODES_FOR_BATCHED_COMMIT_CHECK"]
    codes_correctness = vet_modules["ISSUE_CODES_FOR_CORRECTNESS_CHECK"]
    guides_by_code = vet_modules["ISSUE_IDENTIFICATION_GUIDES_BY_ISSUE_CODE"]

    seen: set = set()
    codes = []
    for code in (*codes_batch, *codes_correctness):
        if code not in seen:
            seen.add(code)
            codes.append(code)

    sections: list[str] = [BRANCH_PREAMBLE]
    for code in codes:
        sections.append(format_guide_section(guides_by_code[code]))
    return "\n".join(sections)


def generate_conversation_categories(vet_modules) -> str:
    """Generate conversation categories from vet."""
    codes = vet_modules["ISSUE_CODES_FOR_CONVERSATION_HISTORY_CHECK"]
    guides_by_code = vet_modules["ISSUE_IDENTIFICATION_GUIDES_BY_ISSUE_CODE"]

    sections: list[str] = [CONVERSATION_PREAMBLE]
    for code in codes:
        sections.append(format_guide_section(guides_by_code[code]))
    sections.append(CONVERSATION_OUTPUT_FORMAT)
    return "\n".join(sections)


def load_vet(vet_repo: Path) -> dict:
    """Import vet modules and return the symbols we need."""
    vet_str = str(vet_repo)
    if vet_str not in sys.path:
        sys.path.insert(0, vet_str)

    from vet.issue_identifiers.identification_guides import ISSUE_CODES_FOR_BATCHED_COMMIT_CHECK
    from vet.issue_identifiers.identification_guides import ISSUE_CODES_FOR_CONVERSATION_HISTORY_CHECK
    from vet.issue_identifiers.identification_guides import ISSUE_CODES_FOR_CORRECTNESS_CHECK
    from vet.issue_identifiers.identification_guides import ISSUE_IDENTIFICATION_GUIDES_BY_ISSUE_CODE

    return {
        "ISSUE_CODES_FOR_BATCHED_COMMIT_CHECK": ISSUE_CODES_FOR_BATCHED_COMMIT_CHECK,
        "ISSUE_CODES_FOR_CORRECTNESS_CHECK": ISSUE_CODES_FOR_CORRECTNESS_CHECK,
        "ISSUE_CODES_FOR_CONVERSATION_HISTORY_CHECK": ISSUE_CODES_FOR_CONVERSATION_HISTORY_CHECK,
        "ISSUE_IDENTIFICATION_GUIDES_BY_ISSUE_CODE": ISSUE_IDENTIFICATION_GUIDES_BY_ISSUE_CODE,
    }


# ---------------------------------------------------------------------------
# Shared check/write logic
# ---------------------------------------------------------------------------


def check_or_write(targets: dict[str, tuple[Path, str]], *, check: bool) -> bool:
    """Check or write a set of targets. Returns True if all OK."""
    ok = True
    for label, (path, content) in targets.items():
        rel = path.relative_to(REPO_ROOT)
        if check:
            if not path.exists():
                print(f"MISSING {rel}", file=sys.stderr)
                ok = False
            elif path.read_text() != content:
                print(f"STALE   {rel}", file=sys.stderr)
                ok = False
            else:
                print(f"OK      {rel}", file=sys.stderr)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text() if path.exists() else None
            if content != existing:
                path.write_text(content)
                print(f"Updated: {rel}", file=sys.stderr)
            else:
                print(f"OK:      {rel}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_vet_repo(explicit: Path | None) -> Path | None:
    """Resolve the vet repo path from explicit arg or env var."""
    if explicit is not None:
        return explicit
    env = os.environ.get("VET_REPO")
    if env:
        return Path(env)
    return None


def cmd_assemble(args: argparse.Namespace) -> None:
    targets = {label: (path, gen_fn()) for label, (path, gen_fn) in ASSEMBLE_TARGETS.items()}
    ok = check_or_write(targets, check=args.check)
    if args.check and not ok:
        print("Run 'uv run python scripts/generate_verify_skills.py assemble' to regenerate.", file=sys.stderr)
        raise SystemExit(1)


def cmd_from_vet(args: argparse.Namespace) -> None:
    vet_repo = _resolve_vet_repo(args.vet_repo)
    if vet_repo is None:
        print(
            "error: vet repo not found.\n"
            "\n"
            "You modified a vet-generated file (scripts/verify_skill_sources/branch-categories.md\n"
            "or conversation-categories.md). To validate against vet, set VET_REPO or regenerate\n"
            "with:\n"
            "\n"
            "    uv run python scripts/generate_verify_skills.py from-vet --vet-repo /path/to/vet\n",
            file=sys.stderr,
        )
        raise SystemExit(1)
    vet_repo = vet_repo.resolve()
    if not (vet_repo / "vet").is_dir():
        print(f"error: does not look like a vet checkout: {vet_repo}", file=sys.stderr)
        raise SystemExit(1)

    vet_modules = load_vet(vet_repo)
    targets = {
        "branch-categories": (BRANCH_CATEGORIES_PATH, generate_branch_categories(vet_modules)),
        "conversation-categories": (CONVERSATION_CATEGORIES_PATH, generate_conversation_categories(vet_modules)),
    }
    # Note: both targets are in scripts/. After updating, run `assemble` to propagate to skill files.
    ok = check_or_write(targets, check=args.check)
    if args.check and not ok:
        print(
            "Run 'uv run python scripts/generate_verify_skills.py from-vet --vet-repo <path>' to regenerate.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate and assemble verify skill markdown files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # assemble subcommand
    p_assemble = subparsers.add_parser(
        "assemble",
        help="Assemble final skill files from local source files (no external deps).",
    )
    p_assemble.add_argument("--check", action="store_true", help="Check without writing.")
    p_assemble.set_defaults(func=cmd_assemble)

    # from-vet subcommand
    p_vet = subparsers.add_parser(
        "from-vet",
        help="Regenerate vet-sourced files from a vet checkout.",
    )
    p_vet.add_argument(
        "--vet-repo",
        type=Path,
        default=None,
        help="Path to vet repo checkout. Falls back to VET_REPO env var.",
    )
    p_vet.add_argument("--check", action="store_true", help="Check without writing.")
    p_vet.set_defaults(func=cmd_from_vet)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
