from pathlib import Path

import pytest

from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
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
    assert forms.abbreviation_snake == "mindstem"
    assert forms.abbreviation_kebab == "mindstem"
    assert forms.abbreviation_snake_upper == "MINDSTEM"
    assert forms.abbreviation_pascal == "Mindstem"


def test_derive_name_forms_multi_word_with_abbreviation() -> None:
    forms = derive_name_forms("default-workspace-template", "workspace template")
    assert forms.kebab == "default-workspace-template"
    assert forms.snake == "default_workspace_template"
    assert forms.snake_upper == "DEFAULT_WORKSPACE_TEMPLATE"
    assert forms.title == "Default Workspace Template"
    assert forms.pascal == "DefaultWorkspaceTemplate"
    assert forms.first_word == "default"
    assert forms.abbreviation_snake == "workspace_template"
    assert forms.abbreviation_kebab == "workspace-template"
    assert forms.abbreviation_snake_upper == "WORKSPACE_TEMPLATE"
    assert forms.abbreviation_pascal == "WorkspaceTemplate"


def test_derive_name_forms_abbreviation_defaults_to_name() -> None:
    forms = derive_name_forms("Mind Stem")
    assert forms.abbreviation_snake == "mind_stem"
    assert forms.abbreviation_kebab == "mind-stem"


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
    with pytest.raises(InvalidNewNameError):
        derive_name_forms("fine-name", "fct-ish")


def test_rewrite_text_representative_lines() -> None:
    replacements = build_replacements(derive_name_forms("default-workspace-template", "workspace template"))
    cases = (
        (
            'URL = "https://github.com/imbue-ai/forever-claude-template.git"',
            'URL = "https://github.com/imbue-ai/default-workspace-template.git"',
        ),
        ("DEFAULT_FOREVER_CLAUDE_GIT_URL: Final[str]", "DEFAULT_WORKSPACE_TEMPLATE_GIT_URL: Final[str]"),
        (
            "from imbue.minds.desktop_client.fct_worktree import materialize_paired_fct_worktree",
            "from imbue.minds.desktop_client.workspace_template_worktree import materialize_paired_workspace_template_worktree",
        ),
        (
            'post_host_create_command__extend = ["/usr/local/bin/fct-seed"]',
            'post_host_create_command__extend = ["/usr/local/bin/workspace-template-seed"]',
        ),
        ("docker tag fct:minds-v0.3.5", "docker tag workspace-template:minds-v0.3.5"),
        (
            "FCT_DIR=/abs/path/to/forever-claude-template",
            "WORKSPACE_TEMPLATE_DIR=/abs/path/to/default-workspace-template",
        ),
        (
            "export MNGR=/your/mngr FCT=/your/forever-claude-template",
            "export MNGR=/your/mngr WORKSPACE_TEMPLATE=/your/default-workspace-template",
        ),
        ('sync-vendor-mngr fct="":', 'sync-vendor-mngr workspace_template="":'),
        ('fct_wt="$(pwd)/{{fct}}"', 'workspace_template_wt="$(pwd)/{{workspace_template}}"'),
        ("All FCT-owned paths", "All workspace-template-owned paths"),
        ("pin an FCT tag", "pin a WORKSPACE_TEMPLATE tag"),
        (
            "the FCT template lags; FCT templates stack",
            "the workspace template lags; WORKSPACE_TEMPLATE templates stack",
        ),
        ("the default forever-claude-template repo URL", "the default-workspace-template repo URL"),
        ("Forever Claude runtime state", "Default Workspace Template runtime state"),
        (
            "https://GitHub.com/Imbue-AI/Forever-Claude-Template.git",
            "https://GitHub.com/Imbue-AI/Default-Workspace-Template.git",
        ),
        ("fct: FctTemplateRef,", "workspace_template: WorkspaceTemplateRef,"),
        ("raise FctWorktreeMissingError(path)", "raise WorkspaceTemplateWorktreeMissingError(path)"),
        ("def fct_template_ref(x):", "def workspace_template_ref(x):"),
        ("fctWorktree = 1", "workspaceTemplateWorktree = 1"),
    )
    for old_line, expected in cases:
        new_line, count = rewrite_text(old_line, replacements)
        assert new_line == expected
        assert count > 0


def test_keep_marker_lines_survive_rewrite_and_check(tmp_path: Path) -> None:
    replacements = build_replacements(derive_name_forms("default-workspace-template"))
    text = 'if [ -n "${FCT_DIR:-}" ]; then  # rename:keep\nFCT_DIR=x\n'
    new_text, count = rewrite_text(text, replacements)
    assert new_text == 'if [ -n "${FCT_DIR:-}" ]; then  # rename:keep\nDEFAULT_WORKSPACE_TEMPLATE_DIR=x\n'
    assert count == 1

    _make_git_repo(tmp_path)
    (tmp_path / "guard.sh").write_text('check "${FCT_DIR:-}"  # rename:keep\n')
    run_git_command(tmp_path, "add", "guard.sh")
    assert all(leftover.rel_path != Path("guard.sh") for leftover in find_leftovers(tmp_path))


def test_rewrite_text_leaves_lookalike_words_alone() -> None:
    replacements = build_replacements(derive_name_forms("default-workspace-template", "workspace template"))
    for line in ("no defects here", "fctl is not the abbreviation", "affctx"):
        new_line, count = rewrite_text(line, replacements)
        assert new_line == line
        assert count == 0


def test_full_name_as_abbreviation_cleanups() -> None:
    replacements = build_replacements(derive_name_forms("default-workspace-template"))
    cases = (
        ("def fct_template_ref(x):", "def default_workspace_template_ref(x):"),
        ("a forever-claude-template (FCT) working tree", "a default-workspace-template working tree"),
        ("the default FCT repo URL", "the default workspace template repo URL"),
        ("Default forever-claude-template repo URL", "Default workspace template repo URL"),
        ("fork the forever-claude-template template as", "fork the default-workspace-template as"),
        ("avoid default-workspace-template-template skew", "avoid default-workspace-template skew"),
        ("fct: FctTemplateRef,", "default_workspace_template: DefaultWorkspaceTemplateRef,"),
        ('f"fct:{tag}"', 'f"default_workspace_template:{tag}"'),
    )
    for old_line, expected in cases:
        new_line, _ = rewrite_text(old_line, replacements)
        assert new_line == expected


def test_single_word_name_degrades_to_uniform_token() -> None:
    replacements = build_replacements(derive_name_forms("mindstem"))
    new_line, _ = rewrite_text("fct_worktree FCT_DIR fct-seed fct:tag", replacements)
    assert new_line == "mindstem_worktree MINDSTEM_DIR mindstem-seed mindstem:tag"


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
    init_git_repo(root, initial_commit=False)
    (root / "README.md").write_text("# forever-claude-template\n\nThe FCT template.\n")
    (root / "src").mkdir()
    (root / "src" / "fct_worktree.py").write_text("FCT_DIR = 'x'\n")
    (root / "changelog").mkdir()
    (root / "changelog" / "entry.md").write_text("renamed forever-claude-template\n")
    run_git_command(root, "add", "-A")


def test_end_to_end_apply_is_idempotent_and_checkable(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    replacements = build_replacements(derive_name_forms("default-workspace-template", "workspace template"))

    plan = plan_repo(tmp_path, replacements, include_diffs=False)
    assert {rewrite.rel_path for rewrite in plan.rewrites} == {Path("README.md"), Path("src/fct_worktree.py")}
    assert [(rename.old_rel_path, rename.new_rel_path) for rename in plan.renames] == [
        (Path("src/fct_worktree.py"), Path("src/workspace_template_worktree.py"))
    ]
    assert {entry.rel_path for entry in plan.skipped} == {Path("changelog/entry.md")}

    apply_plan(plan)
    assert (tmp_path / "src" / "workspace_template_worktree.py").read_text() == "WORKSPACE_TEMPLATE_DIR = 'x'\n"
    assert (tmp_path / "README.md").read_text() == "# default-workspace-template\n\nThe workspace template.\n"
    assert (tmp_path / "changelog" / "entry.md").read_text() == "renamed forever-claude-template\n"

    second_plan = plan_repo(tmp_path, replacements, include_diffs=False)
    assert second_plan.rewrites == ()
    assert second_plan.renames == ()

    assert find_leftovers(tmp_path) == ()


def test_find_leftovers_reports_live_references(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    (tmp_path / "src" / "types.py").write_text("class FctTemplateRef: ...\n")
    run_git_command(tmp_path, "add", "-A")
    leftovers = find_leftovers(tmp_path)
    assert {leftover.rel_path for leftover in leftovers} == {
        Path("README.md"),
        Path("src/fct_worktree.py"),
        Path("src/types.py"),
    }


def test_reintroduced_old_file_dropped_when_identical(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    (tmp_path / "src" / "default_workspace_template_worktree.py").write_text("DEFAULT_WORKSPACE_TEMPLATE_DIR = 'x'\n")
    run_git_command(tmp_path, "add", "-A")
    replacements = build_replacements(derive_name_forms("default-workspace-template"))

    plan = plan_repo(tmp_path, replacements, include_diffs=False)
    rename = next(r for r in plan.renames if r.old_rel_path == Path("src/fct_worktree.py"))
    assert rename.target_exists and rename.target_identical

    apply_plan(plan)
    assert not (tmp_path / "src" / "fct_worktree.py").exists()
    assert (
        tmp_path / "src" / "default_workspace_template_worktree.py"
    ).read_text() == "DEFAULT_WORKSPACE_TEMPLATE_DIR = 'x'\n"


def test_reintroduced_old_file_kept_when_different(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    (tmp_path / "src" / "default_workspace_template_worktree.py").write_text("something_else = True\n")
    run_git_command(tmp_path, "add", "-A")
    replacements = build_replacements(derive_name_forms("default-workspace-template"))

    plan = plan_repo(tmp_path, replacements, include_diffs=False)
    rename = next(r for r in plan.renames if r.old_rel_path == Path("src/fct_worktree.py"))
    assert rename.target_exists and not rename.target_identical

    apply_plan(plan)
    assert (tmp_path / "src" / "fct_worktree.py").exists()
    assert (tmp_path / "src" / "default_workspace_template_worktree.py").read_text() == "something_else = True\n"


def test_symlink_that_is_both_renamed_and_retargeted(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    (tmp_path / "fct_link").symlink_to("src/fct_worktree.py")
    run_git_command(tmp_path, "add", "-A")
    replacements = build_replacements(derive_name_forms("default-workspace-template"))

    plan = plan_repo(tmp_path, replacements, include_diffs=False)
    apply_plan(plan)

    link = tmp_path / "default_workspace_template_link"
    assert str(link.readlink()) == "src/default_workspace_template_worktree.py"
    assert not (tmp_path / "fct_link").exists()


def test_symlink_target_rewritten(tmp_path: Path) -> None:
    _make_git_repo(tmp_path)
    (tmp_path / "seed_link").symlink_to("src/fct_worktree.py")
    run_git_command(tmp_path, "add", "-A")
    replacements = build_replacements(derive_name_forms("default-workspace-template"))

    plan = plan_repo(tmp_path, replacements, include_diffs=False)
    assert [(s.rel_path, s.new_target) for s in plan.symlinks] == [
        (Path("seed_link"), "src/default_workspace_template_worktree.py")
    ]

    apply_plan(plan)
    link = tmp_path / "seed_link"
    assert str(link.readlink()) == "src/default_workspace_template_worktree.py"
    assert link.resolve().read_text() == "DEFAULT_WORKSPACE_TEMPLATE_DIR = 'x'\n"
