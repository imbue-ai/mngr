import os
import textwrap
import tomllib
from pathlib import Path

import pytest

from scripts.sync_vendored_skills import SkillSyncOutcome
from scripts.sync_vendored_skills import VendoredSkillSyncError
from scripts.sync_vendored_skills import _run_git
from scripts.sync_vendored_skills import load_manifest
from scripts.sync_vendored_skills import run_sync

_TEST_GIT_IDENTITY = ("-c", "user.name=Vendored Skill Test", "-c", "user.email=vendored-skill-test@example.com")


def _init_upstream_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True)
    _run_git(["init", "--quiet", "-b", "main"], repo_dir)


def _commit_files(repo_dir: Path, content_by_relative_path: dict[str, str]) -> str:
    """Write files, commit them, and return the commit sha."""
    for relative_path, content in content_by_relative_path.items():
        file_path = repo_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
    _run_git(["add", *content_by_relative_path.keys()], repo_dir)
    _run_git([*_TEST_GIT_IDENTITY, "commit", "--quiet", "-m", "Update skill files"], repo_dir)
    return _run_git(["rev-parse", "HEAD"], repo_dir).strip()


def _repo_url(repo_dir: Path) -> str:
    # file:// (rather than a bare path) so `git clone --depth` does a real shallow clone.
    return f"file://{repo_dir}"


def _write_manifest(manifest_path: Path, body: str) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(textwrap.dedent(body))


def test_sync_vendors_new_skill_and_records_pinned_commit(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    script_body = "#!/bin/sh\necho vendored-skill-test-8631\n"
    upstream_commit = _commit_files(upstream_dir, {"SKILL.md": "# Skill\n", "scripts/run.sh": script_body})
    (upstream_dir / "scripts/run.sh").chmod(0o755)
    upstream_commit_with_mode = _commit_files(upstream_dir, {"scripts/run.sh": script_body + "# exec\n"})
    assert upstream_commit != upstream_commit_with_mode

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        """,
    )
    skills_dir = tmp_path / "skills"

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=False, only_skill_names=())

    assert [result.outcome for result in results] == [SkillSyncOutcome.SYNCED]
    assert (skills_dir / "demo-skill" / "SKILL.md").read_text() == "# Skill\n"
    copied_script = skills_dir / "demo-skill" / "scripts" / "run.sh"
    assert copied_script.read_text().endswith("# exec\n")
    assert os.stat(copied_script).st_mode & 0o111, "executable bit must be preserved"
    recorded = tomllib.loads(manifest_path.read_text())
    assert recorded["skills"]["demo-skill"]["commit"] == upstream_commit_with_mode


def test_sync_skips_when_pinned_commit_is_unknown_upstream(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    _commit_files(upstream_dir, {"SKILL.md": "# Upstream version\n"})

    unknown_pin = "0" * 40
    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        commit = "{unknown_pin}"
        """,
    )
    skills_dir = tmp_path / "skills"
    local_skill_file = skills_dir / "demo-skill" / "SKILL.md"
    local_skill_file.parent.mkdir(parents=True)
    local_skill_file.write_text("# Local-only version\n")

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=False, only_skill_names=())

    assert [result.outcome for result in results] == [SkillSyncOutcome.SKIPPED_LOCAL_AHEAD]
    assert local_skill_file.read_text() == "# Local-only version\n"
    recorded = tomllib.loads(manifest_path.read_text())
    assert recorded["skills"]["demo-skill"]["commit"] == unknown_pin


def test_sync_with_force_overwrites_despite_unknown_pin(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    upstream_commit = _commit_files(upstream_dir, {"SKILL.md": "# Upstream version\n"})

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        commit = "{"0" * 40}"
        """,
    )
    skills_dir = tmp_path / "skills"
    local_skill_file = skills_dir / "demo-skill" / "SKILL.md"
    local_skill_file.parent.mkdir(parents=True)
    local_skill_file.write_text("# Local-only version\n")

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=True, only_skill_names=())

    assert [result.outcome for result in results] == [SkillSyncOutcome.SYNCED]
    assert local_skill_file.read_text() == "# Upstream version\n"
    recorded = tomllib.loads(manifest_path.read_text())
    assert recorded["skills"]["demo-skill"]["commit"] == upstream_commit


def test_sync_include_limits_copied_entries(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    _commit_files(
        upstream_dir,
        {"SKILL.md": "# Skill\n", "README.md": "# Readme\n", "install.sh": "#!/bin/sh\n"},
    )

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        include = ["SKILL.md"]
        """,
    )
    skills_dir = tmp_path / "skills"

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=False, only_skill_names=())

    assert [result.outcome for result in results] == [SkillSyncOutcome.SYNCED]
    copied_names = sorted(entry.name for entry in (skills_dir / "demo-skill").iterdir())
    assert copied_names == ["SKILL.md"]


def test_sync_removes_local_files_that_upstream_no_longer_has(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    _commit_files(upstream_dir, {"SKILL.md": "# Skill\n"})

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        """,
    )
    skills_dir = tmp_path / "skills"
    stale_file = skills_dir / "demo-skill" / "stale.md"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("# No longer upstream\n")

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=False, only_skill_names=())

    assert [result.outcome for result in results] == [SkillSyncOutcome.SYNCED]
    assert not stale_file.exists()
    assert (skills_dir / "demo-skill" / "SKILL.md").is_file()


def test_sync_reports_up_to_date_without_recopying_files(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    upstream_commit = _commit_files(upstream_dir, {"SKILL.md": "# Skill\n"})

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        commit = "{upstream_commit}"
        """,
    )
    skills_dir = tmp_path / "skills"
    local_skill_file = skills_dir / "demo-skill" / "SKILL.md"
    local_skill_file.parent.mkdir(parents=True)
    local_skill_file.write_text("# Locally edited\n")

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=False, only_skill_names=())

    # Freshness is judged by the pinned commit alone; a matching pin means no copy happens.
    assert [result.outcome for result in results] == [SkillSyncOutcome.UP_TO_DATE]
    assert local_skill_file.read_text() == "# Locally edited\n"


def test_check_mode_reports_drift_without_copying(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    old_commit = _commit_files(upstream_dir, {"SKILL.md": "# Version 1\n"})
    _commit_files(upstream_dir, {"SKILL.md": "# Version 2\n"})

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.never-synced-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"

        [skills.stale-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        commit = "{old_commit}"
        """,
    )
    skills_dir = tmp_path / "skills"
    manifest_before = manifest_path.read_text()

    results = run_sync(manifest_path, skills_dir, is_check_only=True, is_forced=False, only_skill_names=())

    outcome_by_name = {result.skill_name: result.outcome for result in results}
    assert outcome_by_name == {
        "never-synced-skill": SkillSyncOutcome.NEVER_SYNCED,
        "stale-skill": SkillSyncOutcome.OUT_OF_DATE,
    }
    assert not skills_dir.exists(), "check mode must not create or copy any files"
    assert manifest_path.read_text() == manifest_before, "check mode must not update pins"


def test_sync_copies_only_the_configured_subdirectory(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    _commit_files(
        upstream_dir,
        {
            ".claude/skills/demo-skill/SKILL.md": "# Nested skill\n",
            "unrelated.py": "print('vendored-skill-test-2947')\n",
        },
    )

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        path = ".claude/skills/demo-skill"
        """,
    )
    skills_dir = tmp_path / "skills"

    results = run_sync(manifest_path, skills_dir, is_check_only=False, is_forced=False, only_skill_names=())

    assert [result.outcome for result in results] == [SkillSyncOutcome.SYNCED]
    copied_names = sorted(entry.name for entry in (skills_dir / "demo-skill").iterdir())
    assert copied_names == ["SKILL.md"]
    assert (skills_dir / "demo-skill" / "SKILL.md").read_text() == "# Nested skill\n"


def test_run_sync_raises_on_unknown_skill_filter(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    _commit_files(upstream_dir, {"SKILL.md": "# Skill\n"})

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        """,
    )

    with pytest.raises(VendoredSkillSyncError, match="no-such-skill"):
        run_sync(
            manifest_path,
            tmp_path / "skills",
            is_check_only=True,
            is_forced=False,
            only_skill_names=("no-such-skill",),
        )


def test_load_manifest_raises_on_missing_skills_table(tmp_path: Path) -> None:
    manifest_path = tmp_path / "vendored_skills.toml"
    manifest_path.write_text("# empty manifest\n")

    with pytest.raises(VendoredSkillSyncError, match=r"\[skills\.<name>\]"):
        load_manifest(manifest_path)


def test_sync_raises_when_include_entry_is_missing_upstream(tmp_path: Path) -> None:
    upstream_dir = tmp_path / "upstream"
    _init_upstream_repo(upstream_dir)
    _commit_files(upstream_dir, {"SKILL.md": "# Skill\n"})

    manifest_path = tmp_path / "vendored_skills.toml"
    _write_manifest(
        manifest_path,
        f"""\
        [skills.demo-skill]
        repo = "{_repo_url(upstream_dir)}"
        ref = "main"
        include = ["SKILL.md", "missing-file.md"]
        """,
    )

    with pytest.raises(VendoredSkillSyncError, match="missing-file.md"):
        run_sync(
            manifest_path,
            tmp_path / "skills",
            is_check_only=False,
            is_forced=False,
            only_skill_names=(),
        )
