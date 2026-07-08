"""One-shot migration tool that renames the forever-claude-template repo to a new name.

Given the new name on the command line, derives every case form (kebab, snake,
SNAKE_UPPER, Title, Pascal, and a compact single token that replaces the ``fct``
abbreviation), rewrites all live references across this monorepo and an optional
template checkout, renames files and directories whose names embed the old name,
and can rename the GitHub repo itself.

Dry-run by default; ``--apply`` edits in place. Idempotent: once applied, a rerun
finds nothing to change. ``--check`` verifies that no live references remain.

Reported but never rewritten: changelog entries and consolidated CHANGELOG files,
``specs/`` and ``blueprint/`` (historical records), ``vendor/`` trees (refreshed
via ``just sync-vendor-mngr``), and ``uv.lock`` (regenerate with ``uv lock``).

Usage:
    uv run python scripts/rename_template_repo.py --new-name mindstem
    uv run python scripts/rename_template_repo.py --new-name mindstem \\
        --template-dir ~/Developer/imbue/forever-claude-template --show-diff
    uv run python scripts/rename_template_repo.py --new-name mindstem --apply --rename-github
    uv run python scripts/rename_template_repo.py --new-name mindstem --check
"""

import difflib
import re
import subprocess
from pathlib import Path
from typing import Final

import click
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class RenameToolError(Exception):
    """Base error for the template-rename tool."""


class InvalidNewNameError(RenameToolError, ValueError):
    """Raised when the requested new name cannot produce valid name forms."""


class ExternalCommandError(RenameToolError, RuntimeError):
    """Raised when a git or gh command fails."""


class LeftoverReferencesError(RenameToolError, RuntimeError):
    """Raised by --check when live references to the old name remain."""


_GITHUB_ORG: Final[str] = "imbue-ai"
_OLD_KEBAB: Final[str] = "forever-claude-template"
_OLD_ABBREVIATION: Final[str] = "fct"

# Historical records and derived artifacts: matches inside these are reported
# but never rewritten.
_SKIPPED_TOP_LEVEL_DIRS: Final[frozenset[str]] = frozenset({"specs", "blueprint"})
_SKIPPED_DIR_NAMES: Final[frozenset[str]] = frozenset({"changelog", "vendor"})
_SKIPPED_FILE_NAMES: Final[frozenset[str]] = frozenset({"uv.lock"})

# The tool itself necessarily contains the old tokens (they are its search patterns).
_SELF_PATHS: Final[frozenset[Path]] = frozenset(
    {
        Path("scripts/rename_template_repo.py"),
        Path("scripts/rename_template_repo_test.py"),
    }
)

# The abbreviation must not sit next to another letter or digit, so `fct_worktree`,
# `fct-seed`, and `fct:` match but a word like `defects` never can. Underscores and
# hyphens are deliberately NOT boundaries.
_ABBREVIATION_PREFIX: Final[str] = r"(?<![A-Za-z0-9])"
_ABBREVIATION_SUFFIX: Final[str] = r"(?![A-Za-z0-9])"

# Post-apply leftovers scan: any case of the old name or the old abbreviation.
_LEFTOVER_PATTERN: Final[str] = (
    r"(?i)forever[-_ ]?claude|" + _ABBREVIATION_PREFIX + _OLD_ABBREVIATION + _ABBREVIATION_SUFFIX
)


class NameForms(FrozenModel):
    """Every case form of the new name that the rewrite needs."""

    kebab: str = Field(description="Repo slug, e.g. `mind-stem`")
    snake: str = Field(description="e.g. `mind_stem`")
    snake_upper: str = Field(description="e.g. `MIND_STEM`")
    title: str = Field(description="e.g. `Mind Stem`")
    pascal: str = Field(description="e.g. `MindStem`")
    compact: str = Field(description="Single token replacing `fct`, e.g. `mindstem`")
    compact_upper: str = Field(description="e.g. `MINDSTEM`, replacing `FCT`")


class Replacement(FrozenModel):
    """A single old-form to new-form rewrite rule."""

    label: str = Field(description="The old form, shown in reports")
    pattern: str = Field(description="Regex matching the old form")
    new_text: str = Field(description="Replacement text")


class FileRewrite(FrozenModel):
    """A planned content rewrite of one file."""

    rel_path: Path = Field(description="Path relative to the repo root")
    replacement_count: int = Field(description="Number of individual replacements in the file")
    new_text: str = Field(description="Full rewritten file content")
    diff: str = Field(description="Unified diff against the current content, empty unless requested")


class SkippedFile(FrozenModel):
    """A file that contains matches but is deliberately left untouched."""

    rel_path: Path = Field(description="Path relative to the repo root")
    reason: str = Field(description="Why the file is skipped")
    match_count: int = Field(description="Number of matches left in place")


class PathRename(FrozenModel):
    """A planned `git mv` of a file or directory whose name embeds the old name."""

    old_rel_path: Path = Field(description="Current path relative to the repo root")
    new_rel_path: Path = Field(description="Renamed path relative to the repo root")


class RepoPlan(FrozenModel):
    """Everything the rewrite would change in one repository."""

    repo_root: Path = Field(description="Absolute path to the repository")
    rewrites: tuple[FileRewrite, ...] = Field(description="Planned content rewrites")
    renames: tuple[PathRename, ...] = Field(description="Planned path renames, deepest first")
    skipped: tuple[SkippedFile, ...] = Field(description="Files with matches that are left untouched")
    undecodable: tuple[Path, ...] = Field(description="Tracked files that are not valid UTF-8")


class Leftover(FrozenModel):
    """One remaining live reference found by --check."""

    rel_path: Path = Field(description="Path relative to the repo root")
    line_number: int = Field(description="1-indexed line of the match")
    line_text: str = Field(description="The matching line, stripped")


def derive_name_forms(new_name: str) -> NameForms:
    """Split the human-entered name into words and derive every case form.

    Raises InvalidNewNameError when no words can be derived or the result still
    contains the old name (which would break idempotency).
    """
    with_camel_breaks = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", new_name)
    words = [word.lower() for word in re.split(r"[^A-Za-z0-9]+", with_camel_breaks) if word]
    if not words:
        raise InvalidNewNameError(f"cannot derive a name from {new_name!r}")
    compact = "".join(words)
    if _OLD_ABBREVIATION in compact or "foreverclaude" in compact:
        raise InvalidNewNameError(f"new name {new_name!r} contains the old name, so the rewrite would not converge")
    return NameForms(
        kebab="-".join(words),
        snake="_".join(words),
        snake_upper="_".join(words).upper(),
        title=" ".join(word.capitalize() for word in words),
        pascal="".join(word.capitalize() for word in words),
        compact=compact,
        compact_upper=compact.upper(),
    )


def build_replacements(forms: NameForms) -> tuple[Replacement, ...]:
    """Rewrite rules ordered longest-form-first so short forms only match what longer forms left behind."""
    literal_pairs = (
        ("forever-claude-template", forms.kebab),
        ("forever_claude_template", forms.snake),
        ("FOREVER_CLAUDE_TEMPLATE", forms.snake_upper),
        ("Forever Claude Template", forms.title),
        ("ForeverClaudeTemplate", forms.pascal),
        ("forever-claude", forms.kebab),
        ("forever_claude", forms.snake),
        ("FOREVER_CLAUDE", forms.snake_upper),
        ("Forever Claude", forms.title),
        ("ForeverClaude", forms.pascal),
    )
    literal_rules = tuple(Replacement(label=old, pattern=re.escape(old), new_text=new) for old, new in literal_pairs)
    abbreviation_pairs = (
        ("FCT", forms.compact_upper),
        ("Fct", forms.pascal),
        ("fct", forms.compact),
    )
    abbreviation_rules = tuple(
        Replacement(label=old, pattern=_ABBREVIATION_PREFIX + old + _ABBREVIATION_SUFFIX, new_text=new)
        for old, new in abbreviation_pairs
    )
    return literal_rules + abbreviation_rules


def rewrite_text(text: str, replacements: tuple[Replacement, ...]) -> tuple[str, int]:
    """Apply every rule to the text; returns the new text and the total replacement count."""
    total = 0
    for rule in replacements:
        text, count = re.subn(rule.pattern, lambda _match, new_text=rule.new_text: new_text, text)
        total += count
    return text, total


def skip_reason(rel_path: Path) -> str | None:
    """Why this tracked file must not be rewritten, or None if it is fair game."""
    if rel_path in _SELF_PATHS:
        return "the rename tool itself"
    parts = rel_path.parts
    if parts[0] in _SKIPPED_TOP_LEVEL_DIRS:
        return "historical record (specs/blueprint)"
    if any(part in _SKIPPED_DIR_NAMES for part in parts[:-1]):
        return "changelog entries / vendored tree"
    if "CHANGELOG" in rel_path.name:
        return "consolidated changelog"
    if rel_path.name in _SKIPPED_FILE_NAMES:
        return "lockfile (regenerate with `uv lock`)"
    return None


def _run(command: tuple[str, ...], cwd: Path | None = None) -> str:
    """Run an external command; raises ExternalCommandError on a nonzero exit."""
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ExternalCommandError(f"{' '.join(command)} failed: {completed.stderr.strip()}")
    return completed.stdout


def list_tracked_files(repo_root: Path) -> tuple[Path, ...]:
    """Paths of all git-tracked files, relative to the repo root."""
    stdout = _run(("git", "-C", str(repo_root), "ls-files", "-z"))
    return tuple(Path(entry) for entry in stdout.split("\0") if entry)


def plan_renames(tracked: tuple[Path, ...], replacements: tuple[Replacement, ...]) -> tuple[PathRename, ...]:
    """Renames for every non-skipped file or directory whose own name embeds the old name, deepest first."""
    pairs: dict[Path, Path] = {}
    for path in tracked:
        if skip_reason(path) is not None:
            continue
        for depth in range(1, len(path.parts) + 1):
            prefix = Path(*path.parts[:depth])
            new_name, _ = rewrite_text(prefix.name, replacements)
            if new_name != prefix.name:
                pairs[prefix] = prefix.with_name(new_name)
    ordered = sorted(pairs.items(), key=lambda item: len(item[0].parts), reverse=True)
    return tuple(PathRename(old_rel_path=old, new_rel_path=new) for old, new in ordered)


def plan_repo(repo_root: Path, replacements: tuple[Replacement, ...], include_diffs: bool) -> RepoPlan:
    """Scan one repository and plan every content rewrite and path rename."""
    tracked = list_tracked_files(repo_root)
    rewrites: list[FileRewrite] = []
    skipped: list[SkippedFile] = []
    undecodable: list[Path] = []
    for rel_path in tracked:
        absolute = repo_root / rel_path
        if not absolute.is_file():
            continue
        try:
            text = absolute.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            undecodable.append(rel_path)
            continue
        new_text, count = rewrite_text(text, replacements)
        if count == 0:
            continue
        reason = skip_reason(rel_path)
        if reason is not None:
            skipped.append(SkippedFile(rel_path=rel_path, reason=reason, match_count=count))
            continue
        diff = ""
        if include_diffs:
            diff = "".join(
                difflib.unified_diff(
                    text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=str(rel_path),
                    tofile=str(rel_path),
                )
            )
        rewrites.append(FileRewrite(rel_path=rel_path, replacement_count=count, new_text=new_text, diff=diff))
    return RepoPlan(
        repo_root=repo_root,
        rewrites=tuple(rewrites),
        renames=plan_renames(tracked, replacements),
        skipped=tuple(skipped),
        undecodable=tuple(undecodable),
    )


def apply_plan(plan: RepoPlan) -> None:
    """Write the planned content rewrites, then `git mv` the planned renames (deepest first)."""
    for rewrite in plan.rewrites:
        (plan.repo_root / rewrite.rel_path).write_bytes(rewrite.new_text.encode("utf-8"))
    for rename in plan.renames:
        _run(("git", "-C", str(plan.repo_root), "mv", str(rename.old_rel_path), str(rename.new_rel_path)))


def find_leftovers(repo_root: Path) -> tuple[Leftover, ...]:
    """Live references to the old name that remain in non-skipped tracked files."""
    pattern = re.compile(_LEFTOVER_PATTERN)
    leftovers: list[Leftover] = []
    for rel_path in list_tracked_files(repo_root):
        if skip_reason(rel_path) is not None:
            continue
        absolute = repo_root / rel_path
        if not absolute.is_file():
            continue
        try:
            text = absolute.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                leftovers.append(Leftover(rel_path=rel_path, line_number=line_number, line_text=line.strip()))
    return tuple(leftovers)


def rename_github_repo(forms: NameForms, is_apply: bool) -> None:
    """Rename the GitHub repo; a no-op when it is already renamed.

    `gh repo view` follows GitHub's rename redirect, so the old address resolving
    to a different name means the rename already happened.
    """
    current = _run(("gh", "repo", "view", f"{_GITHUB_ORG}/{_OLD_KEBAB}", "--json", "name", "-q", ".name")).strip()
    if current == forms.kebab:
        click.echo(f"GitHub repo already renamed to {_GITHUB_ORG}/{forms.kebab}; nothing to do.")
        return
    command = ("gh", "repo", "rename", forms.kebab, "--repo", f"{_GITHUB_ORG}/{_OLD_KEBAB}", "--yes")
    if not is_apply:
        click.echo(f"would run: {' '.join(command)}")
        return
    _run(command)
    click.echo(f"renamed GitHub repo {_GITHUB_ORG}/{_OLD_KEBAB} -> {_GITHUB_ORG}/{forms.kebab}")


def _report_plan(plan: RepoPlan, is_apply: bool, should_show_diff: bool) -> None:
    """Print one repository's plan."""
    verb = "rewrote" if is_apply else "would rewrite"
    total = sum(rewrite.replacement_count for rewrite in plan.rewrites)
    click.echo(f"\n== {plan.repo_root}")
    click.echo(f"{verb} {len(plan.rewrites)} files ({total} replacements):")
    for rewrite in plan.rewrites:
        click.echo(f"  {rewrite.replacement_count:5d}  {rewrite.rel_path}")
    if plan.renames:
        verb = "renamed" if is_apply else "would rename"
        click.echo(f"{verb} {len(plan.renames)} paths:")
        for rename in plan.renames:
            click.echo(f"    {rename.old_rel_path} -> {rename.new_rel_path}")
    if plan.skipped:
        left = sum(entry.match_count for entry in plan.skipped)
        click.echo(f"left untouched ({left} matches in {len(plan.skipped)} files):")
        for entry in plan.skipped:
            click.echo(f"  {entry.match_count:5d}  {entry.rel_path}  [{entry.reason}]")
    if plan.undecodable:
        click.echo(f"not valid UTF-8, not scanned: {', '.join(str(path) for path in plan.undecodable)}")
    if should_show_diff:
        for rewrite in plan.rewrites:
            click.echo(rewrite.diff)


def _report_leftovers(repo_root: Path, leftovers: tuple[Leftover, ...]) -> None:
    click.echo(f"\n== {repo_root}")
    if not leftovers:
        click.echo("no live references to the old name remain.")
        return
    click.echo(f"{len(leftovers)} live references remain:")
    for leftover in leftovers:
        click.echo(f"  {leftover.rel_path}:{leftover.line_number}: {leftover.line_text}")


class RenameCliArguments(FrozenModel):
    """Parsed command line arguments for the rename tool."""

    new_name: str = Field(description="Human-entered new repo name, e.g. 'mindstem' or 'Mind Stem'")
    mngr_root: Path = Field(description="Root of the mngr monorepo checkout")
    template_dir: Path | None = Field(description="Optional path to a forever-claude-template checkout")
    is_apply: bool = Field(description="Whether to edit files (False means dry-run)")
    is_check: bool = Field(description="Whether to only scan for remaining live references")
    should_rename_github: bool = Field(description="Whether to include the GitHub repo rename step")
    should_show_diff: bool = Field(description="Whether to print unified diffs of planned rewrites")


@click.command()
@click.option("--new-name", required=True, help="New repo name; all case forms are derived from it.")
@click.option(
    "--mngr-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path(__file__).resolve().parents[1],
    help="Root of the mngr monorepo checkout (defaults to the checkout containing this script).",
)
@click.option(
    "--template-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Path to a forever-claude-template checkout to rewrite as well.",
)
@click.option("--apply/--dry-run", "is_apply", default=False, help="Edit files, or just report (the default).")
@click.option("--check", "is_check", is_flag=True, help="Only scan for remaining live references; exit 1 if any.")
@click.option("--rename-github", "should_rename_github", is_flag=True, help="Also rename the GitHub repo.")
@click.option("--show-diff", "should_show_diff", is_flag=True, help="Print unified diffs of planned rewrites.")
def main(
    new_name: str,
    mngr_root: Path,
    template_dir: Path | None,
    is_apply: bool,
    is_check: bool,
    should_rename_github: bool,
    should_show_diff: bool,
) -> None:
    """Rename the forever-claude-template repo (references, paths, GitHub) to a new name."""
    arguments = RenameCliArguments(
        new_name=new_name,
        mngr_root=mngr_root,
        template_dir=template_dir,
        is_apply=is_apply,
        is_check=is_check,
        should_rename_github=should_rename_github,
        should_show_diff=should_show_diff,
    )
    try:
        _run_from_arguments(arguments)
    except RenameToolError as e:
        raise click.ClickException(str(e)) from e


def _run_from_arguments(arguments: RenameCliArguments) -> None:
    forms = derive_name_forms(arguments.new_name)
    repo_roots = [arguments.mngr_root] + ([arguments.template_dir] if arguments.template_dir else [])
    if arguments.is_check:
        all_leftovers: list[Leftover] = []
        for repo_root in repo_roots:
            leftovers = find_leftovers(repo_root)
            _report_leftovers(repo_root, leftovers)
            all_leftovers.extend(leftovers)
        if all_leftovers:
            raise LeftoverReferencesError(f"{len(all_leftovers)} live references to the old name remain")
        return
    click.echo(f"new name forms: {forms.model_dump()}")
    if arguments.should_rename_github:
        rename_github_repo(forms, arguments.is_apply)
    replacements = build_replacements(forms)
    for repo_root in repo_roots:
        plan = plan_repo(repo_root, replacements, include_diffs=arguments.should_show_diff)
        if arguments.is_apply:
            apply_plan(plan)
        _report_plan(plan, arguments.is_apply, arguments.should_show_diff)
    if arguments.is_apply:
        click.echo(
            "\nfollow-ups: run `uv lock` in the template checkout (its pyproject name changed), "
            "refresh vendor/mngr via `just sync-vendor-mngr`, update any personal FCT_DIR env/.env entries, "
            "run the full test suites in both repos, and never create a new repo at the old GitHub name "
            "(it would break the rename redirects)."
        )
    else:
        click.echo(
            "\ndry-run only; rerun with --apply to edit. Recommended order: --rename-github first "
            "(redirects keep everything working), then the template checkout, then this monorepo."
        )


if __name__ == "__main__":
    main()
