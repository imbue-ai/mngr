"""Regenerate vet-sourced issue category files from a checkout of imbue-ai/vet.

Requires --vet-repo or VET_REPO env var.

Output:
  .claude/agents/categories/code-issue-categories.md
  .claude/agents/categories/conversation-issue-categories.md

Usage:
    uv run python scripts/generate_verify_skills.py --vet-repo /path/to/vet
    VET_REPO=/path/to/vet uv run python scripts/generate_verify_skills.py
    uv run python scripts/generate_verify_skills.py --check
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from verify_skill_overrides import CATEGORY_EXTENSIONS
from verify_skill_overrides import NEW_CATEGORIES
from verify_skill_overrides import OverrideAction

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# Output paths (vet-generated, checked in)
BRANCH_CATEGORIES_PATH = REPO_ROOT / ".claude" / "agents" / "categories" / "code-issue-categories.md"
CONVERSATION_CATEGORIES_PATH = REPO_ROOT / ".claude" / "agents" / "categories" / "conversation-issue-categories.md"


# ---------------------------------------------------------------------------
# Intermediate representation for a category section
# ---------------------------------------------------------------------------


@dataclass
class CategorySection:
    issue_code: str
    guide: str
    examples: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Formatting helpers
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


def format_category_section(section: CategorySection) -> str:
    """Format a CategorySection into a markdown section."""
    lines: list[str] = []

    lines.append(f"## {section.issue_code}")
    lines.append("")

    lines.append(section.guide)
    lines.append("")

    if section.examples:
        lines.append("**Examples:**")
        for example in section.examples:
            lines.append(f"- {example}")
        lines.append("")

    if section.exceptions:
        lines.append("**Exceptions:**")
        for exception in section.exceptions:
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


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _vet_guide_to_section(guide) -> CategorySection:
    """Convert a vet IssueIdentificationGuide to a CategorySection."""
    return CategorySection(
        issue_code=guide.issue_code.value,
        guide=guide.guide,
        examples=list(guide.examples),
        exceptions=list(guide.exceptions),
    )


def _apply_overrides(sections: list[CategorySection]) -> list[CategorySection]:
    """Apply mng-specific overrides to the category sections.

    Inserts new categories and extends existing ones with additional guidance.
    """
    sections_by_code: dict[str, CategorySection] = {s.issue_code: s for s in sections}

    # Insert new categories after their specified anchor.
    for issue_code, (guide_text, insert_after) in NEW_CATEGORIES.items():
        new_section = CategorySection(issue_code=issue_code, guide=guide_text)
        # Find the index of the anchor and insert after it.
        anchor_idx = next(
            (i for i, s in enumerate(sections) if s.issue_code == insert_after),
            None,
        )
        if anchor_idx is None:
            msg = f"Override anchor '{insert_after}' not found for new category '{issue_code}'"
            raise ValueError(msg)
        sections.insert(anchor_idx + 1, new_section)
        sections_by_code[issue_code] = new_section

    # Apply extensions to existing categories.
    for override in CATEGORY_EXTENSIONS:
        section = sections_by_code.get(override.issue_code)
        if section is None:
            msg = f"Override target '{override.issue_code}' not found in categories"
            raise ValueError(msg)
        match override.action:
            case OverrideAction.APPEND_GUIDE:
                section.guide = section.guide + "\n" + override.content
            case OverrideAction.APPEND_EXAMPLES:
                section.examples.extend(_split_list_items(override.content))
            case OverrideAction.APPEND_EXCEPTIONS:
                section.exceptions.extend(_split_list_items(override.content))
            case OverrideAction.ADD_CATEGORY:
                msg = "ADD_CATEGORY should use NEW_CATEGORIES, not CATEGORY_EXTENSIONS"
                raise ValueError(msg)

    return sections


def _split_list_items(text: str) -> list[str]:
    """Split a block of '- item' lines into individual items (without the leading '- ')."""
    items: list[str] = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:])
        elif stripped:
            items.append(stripped)
    return items


def generate_branch_categories(vet_modules) -> str:
    """Generate branch issue categories from vet, with mng-specific overrides applied."""
    codes_batch = vet_modules["ISSUE_CODES_FOR_BATCHED_COMMIT_CHECK"]
    codes_correctness = vet_modules["ISSUE_CODES_FOR_CORRECTNESS_CHECK"]
    guides_by_code = vet_modules["ISSUE_IDENTIFICATION_GUIDES_BY_ISSUE_CODE"]

    seen: set = set()
    codes = []
    for code in (*codes_batch, *codes_correctness):
        if code not in seen:
            seen.add(code)
            codes.append(code)

    sections = [_vet_guide_to_section(guides_by_code[code]) for code in codes]
    sections = _apply_overrides(sections)

    parts: list[str] = [BRANCH_PREAMBLE]
    for section in sections:
        parts.append(format_category_section(section))
    return "\n".join(parts)


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
# Check/write logic
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate vet-sourced issue category files.",
    )
    parser.add_argument(
        "--vet-repo",
        type=Path,
        default=None,
        help="Path to vet repo checkout. Falls back to VET_REPO env var.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that on-disk files match what would be generated. Exits non-zero if stale.",
    )
    args = parser.parse_args()

    vet_repo = _resolve_vet_repo(args.vet_repo)
    if vet_repo is None:
        print(
            "error: vet repo not found.\n"
            "\n"
            "You modified a vet-generated file (.claude/agents/categories/code-issue-categories.md\n"
            "or conversation-issue-categories.md). To validate against vet,\n"
            "set VET_REPO or regenerate with:\n"
            "\n"
            "    uv run python scripts/generate_verify_skills.py --vet-repo /path/to/vet\n",
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
    ok = check_or_write(targets, check=args.check)
    if args.check and not ok:
        print(
            "Run 'uv run python scripts/generate_verify_skills.py --vet-repo <path>' to regenerate.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
