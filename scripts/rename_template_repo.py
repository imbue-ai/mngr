"""One-shot migration tool that renames the forever-claude-template repo to a new name.

Given the new name (and optionally a shorthand that replaces the ``fct``
abbreviation), derives every case form, rewrites all live references across this
monorepo and an optional template checkout, renames files and directories whose
names embed the old name, and can rename the GitHub repo itself.

The shorthand is applied context-sensitively: snake_case next to ``_`` and for
bare identifiers (``$FCT``, ``fct=""``), kebab-case next to ``-`` or ``:``
(``fct-seed``, ``fct:<tag>``). A cleanup pass collapses word duplication the
rename introduces (``DEFAULT_DEFAULT_WORKSPACE_TEMPLATE`` ->
``DEFAULT_WORKSPACE_TEMPLATE``) and fixes a/an article agreement.

Dry-run by default; ``--apply`` edits in place. Idempotent: once applied, a rerun
finds nothing to change. ``--check`` verifies that no live references remain.

Open branches: run this script from the branch worktree and commit BEFORE merging
main, so both sides use the new names and only real conflicts remain. When a merge
reintroduces an old-name file next to its renamed twin, apply drops the old file if
the rewritten content matches and warns (keeping both) if it differs. Symlinks whose
targets embed the old name get their targets rewritten. On apply with
``--template-dir``, uv.lock is regenerated there automatically.

Reported but never rewritten: changelog entries and consolidated CHANGELOG files,
``specs/`` and ``blueprint/`` (historical records), ``vendor/`` trees (refreshed
via ``just sync-vendor-mngr``), and ``uv.lock`` (regenerate with ``uv lock``).

Usage:
    uv run python scripts/rename_template_repo.py --new-name default-workspace-template \\
        --new-abbreviation "workspace template"
    uv run python scripts/rename_template_repo.py --new-name default-workspace-template \\
        --new-abbreviation "workspace template" \\
        --template-dir ~/Developer/imbue/forever-claude-template --show-diff
    uv run python scripts/rename_template_repo.py --new-name default-workspace-template \\
        --new-abbreviation "workspace template" --apply --rename-github
    uv run python scripts/rename_template_repo.py --new-name default-workspace-template --check
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
# Lockfiles hold base64 hashes where blind replacement corrupts integrity
# checks; regenerate them instead of rewriting.
_SKIPPED_FILE_NAMES: Final[frozenset[str]] = frozenset({"uv.lock", "pnpm-lock.yaml", "package-lock.json"})

# The tool and the state-migration script necessarily contain the old tokens
# (they are their search patterns).
_SELF_PATHS: Final[frozenset[Path]] = frozenset(
    {
        Path("scripts/rename_template_repo.py"),
        Path("scripts/rename_template_repo_test.py"),
        Path("scripts/migrate_state_fct_to_default_workspace_template.sh"),
    }
)

# The abbreviation must not sit next to another letter or digit, so `fct_worktree`,
# `fct-seed`, and `fct:` match but a word like `defects` never can. Underscores and
# hyphens are deliberately NOT boundaries.
_ABBREVIATION_PREFIX: Final[str] = r"(?<![A-Za-z0-9])"
_ABBREVIATION_SUFFIX: Final[str] = r"(?![A-Za-z0-9])"

# Post-apply leftovers scan: any case of the old name or the old abbreviation,
# including CamelCase-embedded forms (FctTemplateRef).
_LEFTOVER_PATTERN: Final[str] = (
    r"(?i:forever[-_ ]?claude)"
    + "|"
    + _ABBREVIATION_PREFIX
    + "(?i:"
    + _OLD_ABBREVIATION
    + ")"
    + _ABBREVIATION_SUFFIX
    + "|"
    + _ABBREVIATION_PREFIX
    + r"[Ff]ct(?=[A-Z])"
)

_VOWELS: Final[frozenset[str]] = frozenset("aeiou")


class NameForms(FrozenModel):
    """Every case form of the new name and its shorthand that the rewrite needs."""

    kebab: str = Field(description="Repo slug, e.g. `default-workspace-template`")
    snake: str = Field(description="e.g. `default_workspace_template`")
    snake_upper: str = Field(description="e.g. `DEFAULT_WORKSPACE_TEMPLATE`")
    title: str = Field(description="e.g. `Default Workspace Template`")
    pascal: str = Field(description="e.g. `DefaultWorkspaceTemplate`")
    first_word: str = Field(description="First word of the name, used by the duplication cleanup")
    abbreviation_snake: str = Field(description="Replaces `fct` next to `_` and bare, e.g. `workspace_template`")
    abbreviation_kebab: str = Field(description="Replaces `fct` next to `-` or `:`, e.g. `workspace-template`")
    abbreviation_snake_upper: str = Field(description="Replaces `FCT`, e.g. `WORKSPACE_TEMPLATE`")
    abbreviation_pascal: str = Field(description="Replaces `Fct`, e.g. `WorkspaceTemplate`")


class Replacement(FrozenModel):
    """A single old-form to new-form rewrite rule."""

    label: str = Field(description="The old form, shown in reports")
    pattern: str = Field(description="Regex matching the old form")
    new_text: str = Field(description="Replacement text")
    hyphen_adjacent_text: str | None = Field(
        default=None, description="Used instead of new_text when the match touches `-` or `:`"
    )


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
    target_exists: bool = Field(
        default=False, description="The new path already exists (an old-name file reintroduced by a merge)"
    )
    target_identical: bool = Field(
        default=False, description="The rewritten old file matches the existing target byte-for-byte"
    )


class SymlinkFix(FrozenModel):
    """A tracked symlink whose target path embeds the old name."""

    rel_path: Path = Field(description="Symlink path relative to the repo root")
    old_target: str = Field(description="Current symlink target")
    new_target: str = Field(description="Rewritten symlink target")


class RepoPlan(FrozenModel):
    """Everything the rewrite would change in one repository."""

    repo_root: Path = Field(description="Absolute path to the repository")
    rewrites: tuple[FileRewrite, ...] = Field(description="Planned content rewrites")
    renames: tuple[PathRename, ...] = Field(description="Planned path renames, deepest first")
    symlinks: tuple[SymlinkFix, ...] = Field(description="Symlinks whose targets need rewriting")
    skipped: tuple[SkippedFile, ...] = Field(description="Files with matches that are left untouched")
    undecodable: tuple[Path, ...] = Field(description="Tracked files that are not valid UTF-8")


class Leftover(FrozenModel):
    """One remaining live reference found by --check."""

    rel_path: Path = Field(description="Path relative to the repo root")
    line_number: int = Field(description="1-indexed line of the match")
    line_text: str = Field(description="The matching line, stripped")


def _split_words(name: str) -> tuple[str, ...]:
    """Lowercased words of a name, split on separators and camelCase humps."""
    with_camel_breaks = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return tuple(word.lower() for word in re.split(r"[^A-Za-z0-9]+", with_camel_breaks) if word)


def derive_name_forms(new_name: str, new_abbreviation: str | None = None) -> NameForms:
    """Derive every case form of the new name and its shorthand.

    The shorthand replaces the old ``fct`` abbreviation and defaults to the full
    name. Raises InvalidNewNameError when no words can be derived or the result
    still contains the old name (which would break idempotency).
    """
    words = _split_words(new_name)
    abbreviation_words = _split_words(new_abbreviation) if new_abbreviation is not None else words
    if not words or not abbreviation_words:
        raise InvalidNewNameError(f"cannot derive a name from {new_name!r} / {new_abbreviation!r}")
    for joined in ("".join(words), "".join(abbreviation_words)):
        if _OLD_ABBREVIATION in joined or "foreverclaude" in joined:
            raise InvalidNewNameError(
                f"new name {new_name!r} / {new_abbreviation!r} contains the old name, "
                "so the rewrite would not converge"
            )
    return NameForms(
        kebab="-".join(words),
        snake="_".join(words),
        snake_upper="_".join(words).upper(),
        title=" ".join(word.capitalize() for word in words),
        pascal="".join(word.capitalize() for word in words),
        first_word=words[0],
        abbreviation_snake="_".join(abbreviation_words),
        abbreviation_kebab="-".join(abbreviation_words),
        abbreviation_snake_upper="_".join(abbreviation_words).upper(),
        abbreviation_pascal="".join(word.capitalize() for word in abbreviation_words),
    )


def _duplication_cleanup_rules(forms: NameForms) -> tuple[Replacement, ...]:
    """Collapse first-word duplication the rename introduces.

    `DEFAULT_FOREVER_CLAUDE_GIT_URL` first becomes
    `DEFAULT_DEFAULT_WORKSPACE_TEMPLATE_GIT_URL`; these rules reduce it to
    `DEFAULT_WORKSPACE_TEMPLATE_GIT_URL`. The duplicated forms contain the new
    name, so they cannot pre-exist and the cleanup stays idempotent.
    """
    first = forms.first_word
    identifier_pairs = (
        (f"{first}-{forms.kebab}", forms.kebab),
        (f"{first} {forms.kebab}", forms.kebab),
        (f"{first}_{forms.snake}", forms.snake),
        (f"{first.upper()}_{forms.snake_upper}", forms.snake_upper),
        (f"{first.capitalize()} {forms.title}", forms.title),
        (f"{first.capitalize()}{forms.pascal}", forms.pascal),
    )
    rules = [Replacement(label=old, pattern=re.escape(old), new_text=new) for old, new in identifier_pairs]
    # Prose like "the FCT template" first becomes "the WORKSPACE_TEMPLATE
    # template"; collapse the duplicated trailing word to "workspace template".
    # The \b keeps plurals ("... templates") out; identifier rules above must
    # stay boundary-free to match inside longer names like ..._GIT_URL.
    if "_" in forms.abbreviation_snake:
        spaced = forms.abbreviation_snake.replace("_", " ")
        last_word = spaced.rsplit(" ", 1)[-1]
        for old in (f"{forms.abbreviation_snake_upper} {last_word}", f"{forms.abbreviation_snake} {last_word}"):
            rules.append(Replacement(label=old, pattern=re.escape(old) + r"\b", new_text=spaced))
        # CamelCase tail duplication: FctTemplateRef -> DefaultWorkspaceTemplateTemplateRef
        # -> DefaultWorkspaceTemplateRef.
        pascal_doubled = f"{forms.abbreviation_pascal}{last_word.capitalize()}"
        rules.append(
            Replacement(label=pascal_doubled, pattern=re.escape(pascal_doubled), new_text=forms.abbreviation_pascal)
        )
        # Identifier tail duplication: fct_template_ref -> default_workspace_template_template_ref
        # -> default_workspace_template_ref (and the kebab equivalent in prose/paths).
        snake_doubled = f"{forms.abbreviation_snake}_{last_word}"
        rules.append(
            Replacement(label=snake_doubled, pattern=re.escape(snake_doubled), new_text=forms.abbreviation_snake)
        )
        kebab_doubled = f"{forms.abbreviation_kebab}-{last_word}"
        rules.append(
            Replacement(label=kebab_doubled, pattern=re.escape(kebab_doubled), new_text=forms.abbreviation_kebab)
        )
        kebab_spaced = f"{forms.abbreviation_kebab} {last_word}"
        rules.append(
            Replacement(
                label=kebab_spaced, pattern=re.escape(kebab_spaced) + r"\b(?!-)", new_text=forms.abbreviation_kebab
            )
        )
    # A parenthetical that used to define the abbreviation now repeats the name:
    # "default-workspace-template (DEFAULT_WORKSPACE_TEMPLATE)" -> the name alone.
    redundant_parenthetical = f"{forms.kebab} ({forms.abbreviation_snake_upper})"
    rules.append(
        Replacement(label=redundant_parenthetical, pattern=re.escape(redundant_parenthetical), new_text=forms.kebab)
    )
    # Cross-case first-word duplication in prose: "the default DEFAULT_WORKSPACE_TEMPLATE repo"
    # and "Default default-workspace-template repo" read as stutters; use the spoken form.
    name_words = forms.snake.split("_")
    if len(name_words) > 1:
        spoken_tail = " ".join(name_words[1:])
        rules.append(
            Replacement(
                label=f"{first} {forms.snake_upper}",
                pattern=re.escape(f"{first} {forms.snake_upper}") + r"(?![A-Za-z0-9_])",
                new_text=f"{first} {spoken_tail}",
            )
        )
        rules.append(
            Replacement(
                label=f"{first.capitalize()} {forms.kebab}",
                pattern=re.escape(f"{first.capitalize()} {forms.kebab}") + r"(?![A-Za-z0-9-])",
                new_text=f"{first.capitalize()} {spoken_tail}",
            )
        )
    return tuple(rules)


def _article_agreement_rules(forms: NameForms) -> tuple[Replacement, ...]:
    """Fix `a`/`an` before tokens the rename introduces (vowel-letter heuristic)."""
    tokens = {
        forms.kebab,
        forms.title,
        forms.abbreviation_snake,
        forms.abbreviation_kebab,
        forms.abbreviation_snake_upper,
    }
    rules: list[Replacement] = []
    for token in sorted(tokens):
        starts_with_vowel = token[0].lower() in _VOWELS
        wrong, right = ("a", "an") if starts_with_vowel else ("an", "a")
        for wrong_cased, right_cased in ((wrong, right), (wrong.capitalize(), right.capitalize())):
            old = f"{wrong_cased} {token}"
            rules.append(
                Replacement(
                    label=old, pattern=rf"\b{wrong_cased} {re.escape(token)}\b", new_text=f"{right_cased} {token}"
                )
            )
    return tuple(rules)


def build_replacements(forms: NameForms) -> tuple[Replacement, ...]:
    """Rewrite rules ordered longest-form-first so short forms only match what longer forms left behind."""
    train = forms.title.replace(" ", "-")
    literal_pairs = (
        ("forever-claude-template", forms.kebab),
        ("forever_claude_template", forms.snake),
        ("FOREVER_CLAUDE_TEMPLATE", forms.snake_upper),
        ("Forever Claude Template", forms.title),
        ("Forever-Claude-Template", train),
        ("ForeverClaudeTemplate", forms.pascal),
        ("forever-claude", forms.kebab),
        ("forever_claude", forms.snake),
        ("FOREVER_CLAUDE", forms.snake_upper),
        ("Forever Claude", forms.title),
        ("ForeverClaude", forms.pascal),
    )
    literal_rules = tuple(Replacement(label=old, pattern=re.escape(old), new_text=new) for old, new in literal_pairs)
    abbreviation_triples = (
        ("FCT", forms.abbreviation_snake_upper, forms.abbreviation_kebab),
        ("Fct", forms.abbreviation_pascal, forms.abbreviation_kebab),
        ("fct", forms.abbreviation_snake, forms.abbreviation_kebab),
    )
    abbreviation_rules = tuple(
        Replacement(
            label=old,
            pattern=_ABBREVIATION_PREFIX + old + _ABBREVIATION_SUFFIX,
            new_text=new,
            hyphen_adjacent_text=hyphen_adjacent,
        )
        for old, new, hyphen_adjacent in abbreviation_triples
    )
    # CamelCase-embedded abbreviation (`FctTemplateRef`, `fctWorktree`): the
    # generic boundary rules skip these because the next character is a letter.
    abbreviation_words = forms.abbreviation_snake.split("_")
    camel_lower = abbreviation_words[0] + "".join(word.capitalize() for word in abbreviation_words[1:])
    camel_rules = (
        Replacement(
            label="Fct (CamelCase)",
            pattern=_ABBREVIATION_PREFIX + r"Fct(?=[A-Z])",
            new_text=forms.abbreviation_pascal,
        ),
        Replacement(
            label="fct (camelCase)",
            pattern=_ABBREVIATION_PREFIX + r"fct(?=[A-Z])",
            new_text=camel_lower,
        ),
    )
    abbreviation_rules = abbreviation_rules + camel_rules
    return literal_rules + abbreviation_rules + _duplication_cleanup_rules(forms) + _article_agreement_rules(forms)


def _substitute(text: str, rule: Replacement) -> tuple[str, int]:
    """Apply one rule, choosing the kebab form when the match touches `-` or `:`."""

    def _pick(match: re.Match[str]) -> str:
        if rule.hyphen_adjacent_text is None:
            return rule.new_text
        preceding = match.string[match.start() - 1] if match.start() > 0 else ""
        following = match.string[match.end()] if match.end() < len(match.string) else ""
        if preceding == "-" or following == "-":
            return rule.hyphen_adjacent_text
        # `fct:<tag>` (docker image ref) is kebab, but `fct: Type` (a Python
        # annotation) and a bare `fct:` key are identifiers.
        after_colon = match.string[match.end() + 1] if match.end() + 1 < len(match.string) else ""
        if following == ":" and after_colon.isalnum():
            return rule.hyphen_adjacent_text
        return rule.new_text

    return re.subn(rule.pattern, _pick, text)


# Lines carrying this marker are deliberate old-name references (legacy-var
# guards, migration hints) that must survive rewrites and --check.
_KEEP_MARKER: Final[str] = "rename:keep"


def rewrite_text(text: str, replacements: tuple[Replacement, ...]) -> tuple[str, int]:
    """Apply every rule to the text; returns the new text and the total replacement count.

    Lines containing the keep marker are left untouched.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [text]
    total = 0
    rewritten: list[str] = []
    for line in lines:
        if _KEEP_MARKER in line:
            rewritten.append(line)
            continue
        for rule in replacements:
            line, count = _substitute(line, rule)
            total += count
        rewritten.append(line)
    return "".join(rewritten), total


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


_COMMAND_TIMEOUT_SECONDS: Final[float] = 600.0


def _run(command: tuple[str, ...], cwd: Path | None = None) -> str:
    """Run an external command; raises ExternalCommandError on a nonzero exit, a missing binary, or a hang."""
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_SECONDS)
    except FileNotFoundError as e:
        raise ExternalCommandError(f"{command[0]} is not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ExternalCommandError(f"{' '.join(command)} timed out after {_COMMAND_TIMEOUT_SECONDS}s") from e
    if completed.returncode != 0:
        raise ExternalCommandError(f"{' '.join(command)} failed: {completed.stderr.strip()}")
    return completed.stdout


def list_tracked_files(repo_root: Path) -> tuple[Path, ...]:
    """Paths of all git-tracked files, relative to the repo root."""
    stdout = _run(("git", "-C", str(repo_root), "ls-files", "-z"))
    return tuple(Path(entry) for entry in stdout.split("\0") if entry)


def plan_renames(
    repo_root: Path, tracked: tuple[Path, ...], replacements: tuple[Replacement, ...]
) -> tuple[PathRename, ...]:
    """Renames for every non-skipped file or directory whose own name embeds the old name, deepest first.

    When the target already exists (an old-name file reintroduced by merging a
    pre-rename branch), the rename is marked: identical content (after rewrite)
    means the old file can be dropped; differing content needs a manual merge.
    """
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
    renames: list[PathRename] = []
    for old, new in ordered:
        old_absolute = repo_root / old
        new_absolute = repo_root / new
        target_exists = new_absolute.exists()
        target_identical = False
        if target_exists and old_absolute.is_file() and new_absolute.is_file():
            rewritten_old, _ = rewrite_text(old_absolute.read_bytes().decode("utf-8", errors="replace"), replacements)
            target_identical = rewritten_old == new_absolute.read_bytes().decode("utf-8", errors="replace")
        renames.append(
            PathRename(
                old_rel_path=old, new_rel_path=new, target_exists=target_exists, target_identical=target_identical
            )
        )
    return tuple(renames)


def plan_repo(repo_root: Path, replacements: tuple[Replacement, ...], include_diffs: bool) -> RepoPlan:
    """Scan one repository and plan every content rewrite and path rename."""
    tracked = list_tracked_files(repo_root)
    rewrites: list[FileRewrite] = []
    symlinks: list[SymlinkFix] = []
    skipped: list[SkippedFile] = []
    undecodable: list[Path] = []
    for rel_path in tracked:
        absolute = repo_root / rel_path
        if absolute.is_symlink():
            # Rewriting through a symlink would mutate its target; fix the
            # target path instead (it may embed the old name).
            old_target = str(absolute.readlink())
            new_target, target_count = rewrite_text(old_target, replacements)
            if target_count > 0 and skip_reason(rel_path) is None:
                symlinks.append(SymlinkFix(rel_path=rel_path, old_target=old_target, new_target=new_target))
            continue
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
        renames=plan_renames(repo_root, tracked, replacements),
        symlinks=tuple(symlinks),
        skipped=tuple(skipped),
        undecodable=tuple(undecodable),
    )


def apply_plan(plan: RepoPlan) -> None:
    """Write the planned content rewrites, then apply renames and symlink fixes.

    A rename whose target already exists is resolved by dropping the old file
    when the rewritten content matches the target, and kept (with a warning)
    when it differs -- that needs a manual merge.
    """
    for rewrite in plan.rewrites:
        (plan.repo_root / rewrite.rel_path).write_bytes(rewrite.new_text.encode("utf-8"))
    # Symlink targets are fixed before path renames so a link that is both
    # renamed and retargeted is still at its old path when we rewrite it.
    for symlink in plan.symlinks:
        absolute = plan.repo_root / symlink.rel_path
        absolute.unlink()
        absolute.symlink_to(symlink.new_target)
    for rename in plan.renames:
        if rename.target_exists:
            if rename.target_identical:
                _run(("git", "-C", str(plan.repo_root), "rm", "-q", "-f", str(rename.old_rel_path)))
            else:
                click.echo(
                    f"WARNING: {rename.new_rel_path} already exists and differs from {rename.old_rel_path}; "
                    "kept both -- merge manually."
                )
            continue
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
            if _KEEP_MARKER in line:
                continue
            if pattern.search(line):
                leftovers.append(Leftover(rel_path=rel_path, line_number=line_number, line_text=line.strip()))
    return tuple(leftovers)


def _regenerate_lockfile(repo_root: Path) -> None:
    """Regenerate uv.lock after the pyproject name change; a failure is reported, not fatal."""
    try:
        _run(("uv", "lock"), cwd=repo_root)
    except ExternalCommandError as e:
        click.echo(f"WARNING: `uv lock` failed in {repo_root}; run it manually. ({e})")
        return
    click.echo(f"regenerated uv.lock in {repo_root}")


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
            note = ""
            if rename.target_exists:
                note = (
                    "  [target exists: drop old, identical]"
                    if rename.target_identical
                    else "  [target exists and DIFFERS: manual merge]"
                )
            click.echo(f"    {rename.old_rel_path} -> {rename.new_rel_path}{note}")
    if plan.symlinks:
        verb = "fixed" if is_apply else "would fix"
        click.echo(f"{verb} {len(plan.symlinks)} symlink targets:")
        for symlink in plan.symlinks:
            click.echo(f"    {symlink.rel_path}: {symlink.old_target} -> {symlink.new_target}")
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

    new_name: str = Field(description="Human-entered new repo name, e.g. 'default-workspace-template'")
    new_abbreviation: str | None = Field(description="Shorthand replacing `fct`, e.g. 'workspace template'")
    mngr_root: Path = Field(description="Root of the mngr monorepo checkout")
    template_dir: Path | None = Field(description="Optional path to a forever-claude-template checkout")
    is_apply: bool = Field(description="Whether to edit files (False means dry-run)")
    is_check: bool = Field(description="Whether to only scan for remaining live references")
    should_rename_github: bool = Field(description="Whether to include the GitHub repo rename step")
    should_show_diff: bool = Field(description="Whether to print unified diffs of planned rewrites")


@click.command()
@click.option("--new-name", required=True, help="New repo name; all case forms are derived from it.")
@click.option(
    "--new-abbreviation",
    default=None,
    help="Shorthand that replaces the `fct` abbreviation (defaults to the full name).",
)
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
    new_abbreviation: str | None,
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
        new_abbreviation=new_abbreviation,
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
    forms = derive_name_forms(arguments.new_name, arguments.new_abbreviation)
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
        if arguments.is_apply and repo_root == arguments.template_dir:
            _regenerate_lockfile(repo_root)
    if arguments.is_apply:
        click.echo(
            "\nfollow-ups: refresh vendor/mngr via `just sync-vendor-mngr`, "
            "update any personal FCT_DIR env/.env entries, "
            "run the full test suites in both repos, and never create a new repo at the old GitHub name "
            "(it would break the rename redirects). Open branches: run this script from the branch "
            "worktree and commit BEFORE merging main, so both sides use the new names and only real "
            "conflicts remain."
        )
    else:
        click.echo(
            "\ndry-run only; rerun with --apply to edit. Recommended order: --rename-github first "
            "(redirects keep everything working), then the template checkout, then this monorepo."
        )


if __name__ == "__main__":
    main()
