import subprocess
from pathlib import Path

import pytest

from scripts.rename_template_repo import InvalidNewNameError
from scripts.rename_template_repo import apply_plan
from scripts.rename_template_repo import build_replacements
from scripts.rename_template_repo import derive_name_forms
from scripts.rename_template_repo import find_leftovers
from scripts.rename_template_repo import plan_repo
from scripts.rename_template_repo import rewrite_text
from scripts.rename_template_repo import skip_reason


def test_derive_name_forms_single_word() -> None:
    forms = derive_name_forms("mindstem")
    assert forms.kebab == "mindstem"
    assert forms.snake == "mindstem"
    assert forms.snake_upper == "MINDSTEM"
    assert forms.title == "Mindstem"
    assert forms.pascal == "Mindstem"
    assert forms.compact == "mindstem"
    assert forms.compact_upper == "MINDSTEM"


def test_derive_name_forms_multi_word() -> None:
    forms = derive_name_forms("Mind Stem")
    assert forms.kebab == "mind-stem"
    assert forms.snake == "mind_stem"
    assert forms.snake_upper == "MIND_STEM"
    assert forms.title == "Mind Stem"
    assert forms.pascal == "MindStem"
    assert forms.compact == "mindstem"
    assert forms.compact_upper == "MINDSTEM"


def test_derive_name_forms_splits_camel_case() -> None:
    forms = derive_name_forms("MindStem")
    assert forms.kebab == "mind-stem"
    assert forms.pascal == "MindStem"


def test_derive_name_forms_rejects_empty_and_old_names() -> None:
    with pytest.raises(InvalidNewNameError):
        derive_name_forms("--")
    with pytest.raises(InvalidNewNameError):
        derive_name_forms("fct2")
    with pytest.raises(InvalidNewNameError):
        derive_name_forms("forever-claude-v2")


def test_rewrite_text_representative_lines() -> None:
    replacements = build_replacements(derive_name_forms("mindstem"))
    cases = (
        (
            'URL = "https://github.com/imbue-ai/forever-claude-template.git"',
            'URL = "https://github.com/imbue-ai/mindstem.git"',
        ),
        ("DEFAULT_FOREVER_CLAUDE_GIT_URL: Final[str]", "DEFAULT_MINDSTEM_GIT_URL: Final[str]"),
        (
            "from imbue.minds.desktop_client.fct_worktree import materialize_paired_fct_worktree",
            "from imbue.minds.desktop_client.mindstem_worktree import materialize_paired_mindstem_worktree",
        ),
        (
            'post_host_create_command__extend = ["/usr/local/bin/fct-seed"]',
            'post_host_create_command__extend = ["/usr/local/bin/mindstem-seed"]',
        ),
        ("docker tag fct:minds-v0.3.5", "docker tag mindstem:minds-v0.3.5"),
        ("FCT_DIR=/abs/path/to/forever-claude-template", "MINDSTEM_DIR=/abs/path/to/mindstem"),
        ("the FCT template and the Forever Claude runtime", "the MINDSTEM template and the Mindstem runtime"),
    )
    for old_line, expected in cases:
        new_line, count = rewrite_text(old_line, replacements)
        assert new_line == expected
        assert count > 0


def test_rewrite_text_leaves_lookalike_words_alone() -> None:
    replacements = build_replacements(derive_name_forms("mindstem"))
    for line in ("no defects here", "fctl is not the abbreviation", "affctx"):
        new_line, count = rewrite_text(line, replacements)
        assert new_line == line
        assert count == 0


def test_multi_word_name_keeps_identifiers_single_token() -> None:
    replacements = build_replacements(derive_name_forms("Mind Stem"))
    new_line, _ = rewrite_text("fct_worktree FCT_DIR fct-seed", replacements)
    assert new_line == "mindstem_worktree MINDSTEM_DIR mindstem-seed"


def test_skip_reason() -> None:
    assert skip_reason(Path("specs/foo/spec.md")) is not None
    assert skip_reason(Path("blueprint/foo/plan.md")) is not None
    assert skip_reason(Path("dev/changelog/entry.md")) is not None
    assert skip_reason(Path("vendor/mngr/justfile")) is not None
    assert skip_reason(Path("apps/minds/CHANGELOG.md")) is not None
    assert skip_reason(Path("apps/minds/UNABRIDGED_CHANGELOG.md")) is not None
    assert skip_reason(Path("uv.lock")) is not None
    assert skip_reason(Path("scripts/rename_template_repo.py")) is not None
    assert skip_reason(Path("apps/minds/docs/release.md")) is None
    assert skip_reason(Path("justfile")) is None


def _make_git_repo(root: Path) -> None:
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    (root / "README.md").write_text("# forever-claude-template\n\nThe FCT template.\n")
    (root / "src").mkdir()
    (root / "src" / "fct_worktree.py").write_text("FCT_DIR = 'x'\n")
    (root / "changelog").mkdir()
    (root / "changelog" / "entry.md").write_text("renamed forever-claude-template\n")
    subprocess.run(("git", "add", "-A"), cwd=root, check=True)


def test_end_to_end_apply_is_idempotent_and_checkable(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    replacements = build_replacements(derive_name_forms("mindstem"))

    plan = plan_repo(tmp_path, replacements, include_diffs=False)
    assert {rewrite.rel_path for rewrite in plan.rewrites} == {Path("README.md"), Path("src/fct_worktree.py")}
    assert [(rename.old_rel_path, rename.new_rel_path) for rename in plan.renames] == [
        (Path("src/fct_worktree.py"), Path("src/mindstem_worktree.py"))
    ]
    assert {entry.rel_path for entry in plan.skipped} == {Path("changelog/entry.md")}

    apply_plan(plan)
    assert (tmp_path / "src" / "mindstem_worktree.py").read_text() == "MINDSTEM_DIR = 'x'\n"
    assert (tmp_path / "README.md").read_text() == "# mindstem\n\nThe MINDSTEM template.\n"
    assert (tmp_path / "changelog" / "entry.md").read_text() == "renamed forever-claude-template\n"

    second_plan = plan_repo(tmp_path, replacements, include_diffs=False)
    assert second_plan.rewrites == ()
    assert second_plan.renames == ()

    assert find_leftovers(tmp_path) == ()


def test_find_leftovers_reports_live_references(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    leftovers = find_leftovers(tmp_path)
    assert {leftover.rel_path for leftover in leftovers} == {Path("README.md"), Path("src/fct_worktree.py")}
