"""Vendor Claude Code skills from external git repos into .claude/skills/.

The manifest at .claude/vendored_skills.toml lists each vendored skill: the
upstream repo, the ref to track, an optional subdirectory (`path`) and
top-level `include` list, and the upstream `commit` that was last vendored.
This script refreshes the local copies from upstream and records the newly
vendored commit back into the manifest.

Run from the repo root::

    uv run python -m scripts.sync_vendored_skills            # sync everything
    uv run python -m scripts.sync_vendored_skills --check    # report drift only
    uv run python -m scripts.sync_vendored_skills --skill crispy-comments

Sync never overwrites a local copy whose pinned commit is not found in the
upstream ref's recent history (e.g. the pin references a local change that has
not been pushed or merged upstream yet); pass --force to override.
"""

import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

import click
import tomlkit
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
MANIFEST_PATH: Final[Path] = REPO_ROOT / ".claude" / "vendored_skills.toml"
SKILLS_DIR: Final[Path] = REPO_ROOT / ".claude" / "skills"

# History depth fetched when syncing. The pinned commit must appear within this
# window of the upstream ref for sync to overwrite the local copy without --force.
CLONE_DEPTH: Final[int] = 50
GIT_TIMEOUT_SECONDS: Final[float] = 600.0


class VendoredSkillSyncError(Exception):
    """Base error for vendored-skill sync failures."""


class VendoredSkill(FrozenModel):
    """One vendored skill entry from the manifest."""

    name: str = Field(description="Directory name under .claude/skills/")
    # str rather than AnyUrl: git remotes may be scp-like (git@host:path), which is not a URL.
    repo: str = Field(description="Git URL of the upstream repository")
    ref: str = Field(description="Upstream branch or tag to track")
    path: str = Field(default=".", description="Subdirectory of the upstream repo containing the skill")
    include: tuple[str, ...] | None = Field(
        default=None,
        description="Top-level entries of the skill directory to copy; None copies everything except .git*",
    )
    commit: str | None = Field(default=None, description="Upstream commit last vendored; None if never synced")


class SkillSyncOutcome(UpperCaseStrEnum):
    """What happened to (or is known about) one vendored skill."""

    UP_TO_DATE = auto()
    SYNCED = auto()
    SKIPPED_LOCAL_AHEAD = auto()
    # Check-mode-only outcomes:
    NEVER_SYNCED = auto()
    OUT_OF_DATE = auto()


class SkillSyncResult(FrozenModel):
    """Per-skill result of a sync or check run."""

    skill_name: str = Field(description="Manifest name of the skill")
    outcome: SkillSyncOutcome = Field(description="What happened to this skill")
    remote_commit: str = Field(description="Upstream head commit of the tracked ref")
    detail: str = Field(description="Human-readable elaboration; may be empty")


def _run_git(git_args: Sequence[str], cwd: Path) -> str:
    command = ["git", *git_args]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as e:
        raise VendoredSkillSyncError(f"`{' '.join(command)}` failed: {e.stderr.strip()}") from e
    except subprocess.TimeoutExpired as e:
        raise VendoredSkillSyncError(f"`{' '.join(command)}` timed out after {GIT_TIMEOUT_SECONDS}s") from e
    return completed.stdout


def load_manifest(manifest_path: Path) -> list[VendoredSkill]:
    """Raises VendoredSkillSyncError if the manifest is missing, empty, or malformed."""
    if not manifest_path.is_file():
        raise VendoredSkillSyncError(f"Manifest not found: {manifest_path}")
    raw_manifest = tomllib.loads(manifest_path.read_text())
    skills_table = raw_manifest.get("skills")
    if not isinstance(skills_table, dict) or not skills_table:
        raise VendoredSkillSyncError(f"Manifest {manifest_path} must contain a non-empty [skills.<name>] table")
    return [VendoredSkill.model_validate({"name": name, **entry}) for name, entry in skills_table.items()]


def _resolve_remote_head_commit(skill: VendoredSkill) -> str:
    output = _run_git(["ls-remote", skill.repo, skill.ref], REPO_ROOT)
    for line in output.splitlines():
        commit_sha, _, ref_name = line.partition("\t")
        if ref_name in (f"refs/heads/{skill.ref}", f"refs/tags/{skill.ref}", skill.ref):
            return commit_sha
    raise VendoredSkillSyncError(f"Ref {skill.ref!r} not found in {skill.repo}")


def _is_commit_present_in_clone(clone_dir: Path, commit_sha: str) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return completed.returncode == 0


def _select_source_entries(source_dir: Path, skill: VendoredSkill) -> list[Path]:
    if skill.include is None:
        return sorted(entry for entry in source_dir.iterdir() if not entry.name.startswith(".git"))
    selected_entries: list[Path] = []
    for entry_name in skill.include:
        entry = source_dir / entry_name
        if not entry.exists():
            raise VendoredSkillSyncError(
                f"Include entry {entry_name!r} of skill {skill.name!r} not found in upstream {skill.repo}"
            )
        selected_entries.append(entry)
    return selected_entries


def _copy_skill_files(source_dir: Path, target_dir: Path, skill: VendoredSkill) -> None:
    selected_entries = _select_source_entries(source_dir, skill)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    for entry in selected_entries:
        if entry.is_dir():
            shutil.copytree(entry, target_dir / entry.name)
        else:
            shutil.copy2(entry, target_dir / entry.name)


def sync_one_skill(skill: VendoredSkill, skills_dir: Path, is_forced: bool) -> SkillSyncResult:
    """Bring the local copy of one skill up to the upstream ref head, guarding against clobbering local-only pins."""
    remote_head_commit = _resolve_remote_head_commit(skill)
    if skill.commit == remote_head_commit:
        return SkillSyncResult(
            skill_name=skill.name,
            outcome=SkillSyncOutcome.UP_TO_DATE,
            remote_commit=remote_head_commit,
            detail="",
        )

    with tempfile.TemporaryDirectory(prefix="vendored-skill-") as temp_dir:
        clone_dir = Path(temp_dir) / "clone"
        _run_git(
            ["clone", "--quiet", "--depth", str(CLONE_DEPTH), "--branch", skill.ref, skill.repo, str(clone_dir)],
            REPO_ROOT,
        )

        # Refuse to clobber a local copy whose pin is unknown upstream: that means the
        # local copy carries changes (or tracks a commit) that upstream does not have yet.
        if skill.commit is not None and not is_forced and not _is_commit_present_in_clone(clone_dir, skill.commit):
            return SkillSyncResult(
                skill_name=skill.name,
                outcome=SkillSyncOutcome.SKIPPED_LOCAL_AHEAD,
                remote_commit=remote_head_commit,
                detail=(
                    f"pinned commit {skill.commit[:12]} not found in the last {CLONE_DEPTH} commits of "
                    f"{skill.repo}@{skill.ref}; local copy kept as-is (pass --force to overwrite)"
                ),
            )

        source_dir = (clone_dir / skill.path).resolve()
        if not source_dir.is_dir():
            raise VendoredSkillSyncError(
                f"Path {skill.path!r} of skill {skill.name!r} not found in upstream {skill.repo}@{skill.ref}"
            )
        _copy_skill_files(source_dir=source_dir, target_dir=skills_dir / skill.name, skill=skill)

    return SkillSyncResult(
        skill_name=skill.name,
        outcome=SkillSyncOutcome.SYNCED,
        remote_commit=remote_head_commit,
        detail=f"vendored {remote_head_commit[:12]}",
    )


def check_one_skill(skill: VendoredSkill) -> SkillSyncResult:
    """Compare the manifest pin against the upstream ref head without touching any files."""
    remote_head_commit = _resolve_remote_head_commit(skill)
    if skill.commit is None:
        outcome = SkillSyncOutcome.NEVER_SYNCED
        detail = "no pinned commit in the manifest; run a sync to vendor it"
    elif skill.commit == remote_head_commit:
        outcome = SkillSyncOutcome.UP_TO_DATE
        detail = ""
    else:
        outcome = SkillSyncOutcome.OUT_OF_DATE
        detail = f"pinned {skill.commit[:12]}, upstream {skill.ref} is at {remote_head_commit[:12]}"
    return SkillSyncResult(skill_name=skill.name, outcome=outcome, remote_commit=remote_head_commit, detail=detail)


def _update_manifest_pins(manifest_path: Path, commit_by_skill_name: Mapping[str, str]) -> None:
    document = tomlkit.parse(manifest_path.read_text())
    skills_table = document["skills"]
    if not isinstance(skills_table, dict):
        raise VendoredSkillSyncError(f"Manifest {manifest_path} has a malformed [skills] table")
    for skill_name, commit_sha in commit_by_skill_name.items():
        skill_entry = skills_table[skill_name]
        if not isinstance(skill_entry, dict):
            raise VendoredSkillSyncError(f"Manifest entry for skill {skill_name!r} is malformed")
        skill_entry["commit"] = commit_sha
    manifest_path.write_text(tomlkit.dumps(document))


def run_sync(
    manifest_path: Path,
    skills_dir: Path,
    is_check_only: bool,
    is_forced: bool,
    only_skill_names: Sequence[str],
) -> list[SkillSyncResult]:
    """Sync (or check) every manifest entry, updating manifest pins for skills that were synced."""
    skills = load_manifest(manifest_path)
    if only_skill_names:
        known_names = {skill.name for skill in skills}
        unknown_names = sorted(set(only_skill_names) - known_names)
        if unknown_names:
            raise VendoredSkillSyncError(f"Unknown skill(s) {unknown_names}; manifest has {sorted(known_names)}")
        skills = [skill for skill in skills if skill.name in only_skill_names]

    results: list[SkillSyncResult] = []
    commit_by_synced_skill_name: dict[str, str] = {}
    for skill in skills:
        if is_check_only:
            result = check_one_skill(skill)
        else:
            result = sync_one_skill(skill, skills_dir=skills_dir, is_forced=is_forced)
        results.append(result)
        if result.outcome is SkillSyncOutcome.SYNCED:
            commit_by_synced_skill_name[skill.name] = result.remote_commit

    if commit_by_synced_skill_name:
        _update_manifest_pins(manifest_path, commit_by_synced_skill_name)
    return results


def _format_result_line(result: SkillSyncResult) -> str:
    match result.outcome:
        case SkillSyncOutcome.UP_TO_DATE:
            status_text = "up to date"
        case SkillSyncOutcome.SYNCED:
            status_text = "synced"
        case SkillSyncOutcome.SKIPPED_LOCAL_AHEAD:
            status_text = "SKIPPED (local ahead of upstream)"
        case SkillSyncOutcome.NEVER_SYNCED:
            status_text = "NEVER SYNCED"
        case SkillSyncOutcome.OUT_OF_DATE:
            status_text = "OUT OF DATE"
        case _ as unreachable:
            assert_never(unreachable)
    suffix = f" -- {result.detail}" if result.detail else ""
    return f"{result.skill_name}: {status_text}{suffix}"


@click.command()
@click.option(
    "--check",
    "is_check_only",
    is_flag=True,
    help="Report drift against upstream without copying files or updating pins.",
)
@click.option(
    "--force",
    "is_forced",
    is_flag=True,
    help="Overwrite a local copy even when its pinned commit is not found upstream.",
)
@click.option(
    "--skill",
    "only_skill_names",
    multiple=True,
    help="Limit to the named skill(s) from the manifest; may be repeated.",
)
def main(is_check_only: bool, is_forced: bool, only_skill_names: tuple[str, ...]) -> None:
    results = run_sync(
        manifest_path=MANIFEST_PATH,
        skills_dir=SKILLS_DIR,
        is_check_only=is_check_only,
        is_forced=is_forced,
        only_skill_names=only_skill_names,
    )
    for result in results:
        click.echo(_format_result_line(result))

    is_drift_found = any(
        result.outcome in (SkillSyncOutcome.NEVER_SYNCED, SkillSyncOutcome.OUT_OF_DATE) for result in results
    )
    if is_check_only and is_drift_found:
        sys.exit(1)


if __name__ == "__main__":
    main()
