import ast
import fnmatch
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
import tomlkit
import yaml
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RegexRatchetRule
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule
from imbue.imbue_common.ratchet_testing.common_ratchets import check_ratchet_rule_all_files
from imbue.imbue_common.ratchet_testing.core import BINARY_FILE_EXCLUSION
from imbue.imbue_common.ratchet_testing.core import RatchetMatchChunk
from imbue.imbue_common.ratchet_testing.core import _get_all_files_with_extension
from imbue.imbue_common.ratchet_testing.ratchets import check_no_import_lint_errors
from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors
from imbue.imbue_common.ratchet_testing.ratchets import find_bash_scripts_without_strict_mode
from scripts.changelog_projects import all_known_projects
from scripts.changelog_projects import project_dir as get_project_dir
from scripts.changelog_projects import project_entries_dir
from scripts.changelog_projects import pyproject_projects

_REPO_ROOT = Path(__file__).parent

# The public mirror carries only the open-source subset; ratchets over private
# ops assets apply only in the source-of-truth repo (identified by mirror/).
_IS_SOURCE_OF_TRUTH = (_REPO_ROOT / "mirror").exists()

# Projects that are excluded from ratchet requirements (scheduled for deletion).
# Keep in sync with EXCLUDED_RATCHET_PROJECTS in scripts/sync_common_ratchets.py
# (verified by test_excluded_projects_in_sync in scripts/sync_common_ratchets_test.py).
_EXCLUDED_PROJECTS: frozenset[str] = frozenset()

_SELF_EXCLUSION: tuple[str, ...] = ("test_meta_ratchets.py",)
# Machine-generated data files whose contents are not human-written text:
# npm lockfiles carry random base64 integrity hashes that can contain any
# short letter run (e.g. "mng"), so they are excluded from content scans.
_DATA_FILE_EXCLUSION: tuple[str, ...] = ("*.jsonl", "package-lock.json")
_MIGRATION_SCRIPT_EXCLUSION: tuple[str, ...] = (
    "migrate_code_mng_to_mngr.sh",
    "migrate_state_mng_to_mngr.sh",
    "release_tombstones.py",
)

pytestmark = pytest.mark.xdist_group(name="meta_ratchets")


def _get_all_project_dirs() -> list[Path]:
    """Return all project directories (libs/* and apps/*) that are not excluded.

    Built on top of ``pyproject_projects`` (the shared libs/+apps/+pyproject.toml
    discovery helper in ``scripts.changelog_projects``) so this stays in sync
    with the changelog tooling without having to add and then re-remove the
    synthetic ``dev`` bucket.
    """
    return [
        get_project_dir(name, _REPO_ROOT) for name in pyproject_projects(_REPO_ROOT) if name not in _EXCLUDED_PROJECTS
    ]


def _get_workspace_project_dirs() -> list[Path]:
    """Return the project directories that are members of the root uv workspace.

    A project listed in the root ``[tool.uv.workspace].exclude`` is a standalone
    uv project: it has its own lockfile and CI job, the root ``uv sync
    --all-packages`` never installs it, and the root pytest run never collects
    it. Anything that reasons about *root* pytest/coverage configuration must
    look at this list rather than every directory that happens to hold a
    pyproject.toml, otherwise it demands root config for packages that cannot
    be imported there.
    """
    root_pyproject = tomlkit.parse((_REPO_ROOT / "pyproject.toml").read_text())
    excluded_globs = [str(p) for p in root_pyproject["tool"]["uv"]["workspace"].get("exclude", [])]
    # Spell each glob without a trailing slash. Two other readers match this same
    # list with their own matchers -- scripts/utils.py's iter_standalone_project_dirs
    # globs the pattern directly, and scripts/snapshot_minds_e2e_state.py matches
    # `f"{glob}/*"` -- and none of the three normalizes a trailing slash. With one,
    # this function stops recognizing the exclusion, _get_standalone_project_dirs()
    # goes empty, the standalone-project ratchets below pass vacuously, and
    # scripts/release.py quietly stops advancing that project's cooldown cutoff. An
    # absent `exclude` key is fine (the public-mirror overlay has none); only the
    # ambiguous spelling is rejected.
    assert all(not glob.endswith("/") for glob in excluded_globs), (
        "[tool.uv.workspace].exclude entries must not end in '/': "
        f"{[glob for glob in excluded_globs if glob.endswith('/')]}"
    )
    return [
        d
        for d in _get_all_project_dirs()
        if not any(fnmatch.fnmatch(str(d.relative_to(_REPO_ROOT)), glob) for glob in excluded_globs)
    ]


def _get_standalone_project_dirs() -> list[Path]:
    """Return the project directories that are NOT members of the root uv workspace.

    The complement of ``_get_workspace_project_dirs``, expressed as a difference so
    the exclusion globs are read in exactly one place.
    """
    workspace_dirs = set(_get_workspace_project_dirs())
    return [d for d in _get_all_project_dirs() if d not in workspace_dirs]


def _find_test_ratchets_file(project_dir: Path) -> Path | None:
    """Find the test_ratchets.py file within a project directory.

    Only non-gitignored files count: generated artifacts a project keeps in a
    gitignored directory (e.g. apps/minds_evals/datasets/, whose harbor tasks
    embed a full mngr-internal clone) must not be mistaken for project code.
    """
    matches = [f for f in _get_all_files_with_extension(project_dir, ".py") if f.name == "test_ratchets.py"]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) == 0:
        return None
    else:
        raise AssertionError(
            f"Found multiple test_ratchets.py files in {project_dir.name}: "
            + ", ".join(str(m.relative_to(project_dir)) for m in matches)
        )


def _extract_test_function_names(file_path: Path) -> frozenset[str]:
    """Extract all test function names (starting with 'test_') from a Python file using AST."""
    tree = ast.parse(file_path.read_text())
    return frozenset(
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )


# --- Meta: ensure every project has ratchets ---


def test_every_project_has_test_ratchets_file() -> None:
    """Ensure each project (except excluded ones) has a test_ratchets.py file."""
    missing: list[str] = []
    for project_dir in _get_all_project_dirs():
        if _find_test_ratchets_file(project_dir) is None:
            missing.append(project_dir.name)
    assert len(missing) == 0, "The following projects are missing a test_ratchets.py file:\n" + "\n".join(
        f"  - {m}" for m in missing
    )


def _get_expected_ratchet_test_names() -> frozenset[str]:
    """Derive the expected set of test function names from standard_ratchet_checks.py.

    Each check_foo() function maps to test_prevent_foo().
    """
    checks_path = (
        _REPO_ROOT
        / "libs"
        / "imbue_common"
        / "imbue"
        / "imbue_common"
        / "ratchet_testing"
        / "standard_ratchet_checks.py"
    )
    tree = ast.parse(checks_path.read_text())
    test_names = {
        f"test_prevent_{node.name.removeprefix('check_')}"
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("check_")
    }
    return frozenset(test_names)


def test_all_test_ratchets_files_have_same_tests() -> None:
    """Ensure all test_ratchets.py files define precisely the expected set of test functions.

    The expected tests are derived from standard_ratchet_checks.py (one test_prevent_*
    per check_* function).
    """
    reference_tests = _get_expected_ratchet_test_names()

    mismatches: list[str] = []
    for project_dir in _get_all_project_dirs():
        ratchet_file = _find_test_ratchets_file(project_dir)
        if ratchet_file is None:
            continue
        project_tests = _extract_test_function_names(ratchet_file)
        missing_tests = reference_tests - project_tests
        extra_tests = project_tests - reference_tests
        if missing_tests or extra_tests:
            parts = [f"  {project_dir.name} (vs standard_ratchet_checks.py):"]
            if missing_tests:
                parts.append(f"    missing: {sorted(missing_tests)}")
            if extra_tests:
                parts.append(f"    extra:   {sorted(extra_tests)}")
            mismatches.append("\n".join(parts))

    assert len(mismatches) == 0, "test_ratchets.py files have different test functions:\n" + "\n".join(mismatches)


# --- Repo-wide ratchets (run once, not per-project) ---


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_no_import_layer_violations() -> None:
    """Ensure production code has zero import layer violations.

    Runs locally in ~3s but calls grimp's Rust-based import scanner, which
    under CI load occasionally exceeds the default 10s pytest-timeout. When
    the timeout fires via SIGALRM while Rust is scanning, pyo3 raises a
    PanicException that takes down the whole pytest process and drops
    coverage for the sandbox's other tests (see mngr_claude coverage
    regressions on retried PRs). ``@pytest.mark.flaky`` makes offload
    automatically retry if the bump-to-60s still isn't enough.
    """
    check_no_import_lint_errors(_REPO_ROOT)


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_no_import_layer_violations_minds_admin() -> None:
    """Ensure minds_admin production code has zero import layer violations.

    Enforces the ``minds_admin layers contract`` (main > cli > envs > bake >
    slices). See ``test_no_import_layer_violations`` for the flaky/timeout
    rationale.
    """
    check_no_import_lint_errors(_REPO_ROOT, contract_name="minds_admin layers contract")


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_no_import_layer_violations_mngr_imbue_cloud() -> None:
    """Ensure mngr_imbue_cloud production code has zero import layer violations.

    Enforces the ``mngr_imbue_cloud layers contract`` (the sub-package layering:
    plugin > cli > bake > providers > hosts > slices > connector > config >
    data_types > errors > primitives). See ``test_no_import_layer_violations``
    for the flaky/timeout rationale.
    """
    check_no_import_lint_errors(_REPO_ROOT, contract_name="mngr_imbue_cloud layers contract")


@pytest.mark.timeout(60)
def test_no_type_errors() -> None:
    """Ensure the whole workspace has zero type errors (ty).

    ty resolves the uv workspace root (root pyproject.toml declares
    [tool.uv.workspace] members = ["libs/*", "apps/*"]) and scans every member, so
    this single check covers the entire repo. CI backstop for the ty pre-push hook.

    Timeout is 60s rather than the default 10s because the ``uv run ty check``
    subprocess can be slow on offload under load; the check is deterministic, so it
    is not marked flaky. If a failure looks spurious, run ``uv sync --all-packages``
    and re-run before treating it as real (see CLAUDE.md).
    """
    check_no_type_errors(_REPO_ROOT)


def test_no_ruff_errors() -> None:
    """Ensure all Python files pass ruff lint and format checks repo-wide.

    Runs both ruff check and ruff format --check from the repo root, covering all
    workspace members plus repo-root and scripts/ files. CI backstop for the ruff
    pre-commit hook.
    """
    fix_hint = "To fix: `uv run ruff check --fix . && uv run ruff format .`"
    errors: list[str] = []

    lint = subprocess.run(
        ["uv", "run", "ruff", "check", "--force-exclude", "--config", "pyproject.toml"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if lint.returncode != 0:
        errors.append("Lint errors:\n" + lint.stdout)

    fmt = subprocess.run(
        ["uv", "run", "ruff", "format", "--check", "--force-exclude", "--config", "pyproject.toml"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if fmt.returncode != 0:
        errors.append("Format errors:\n" + fmt.stdout)

    if errors:
        raise AssertionError("\n".join(errors) + "\n" + fix_hint)


# Regenerating every command's docs spawns a fresh interpreter with all plugins loaded,
# which takes several seconds locally and exceeds the default 10s pytest-timeout in the
# slower offload sandbox (the bare-metal `admin server` + slice commands enlarged the CLI
# surface). Match the other heavy meta-ratchet tests with a generous timeout.
@pytest.mark.timeout(60)
def test_cli_docs_are_up_to_date() -> None:
    """Committed CLI docs and the PyPI README must match scripts/make_cli_docs.py output.

    Guards against editing a command's help metadata (or the top-level README) without
    regenerating the docs -- the same check the regenerate-cli-docs pre-commit hook performs.
    This complements test_all_non_hidden_commands_have_generated_docs in help_formatter_test.py
    (which only checks that a doc *file* exists per command) by verifying the file *contents*
    are current.

    The generator is run via its --check mode in a fresh interpreter so that
    MNGR_LOAD_ALL_PLUGINS is set before any mngr import and every provider's commands are
    documented; running it in-process would not reliably reload already-imported modules.
    """
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "make_cli_docs.py"), "--check"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Generated CLI docs are out of date. Run `uv run python scripts/make_cli_docs.py` "
        f"and commit the result.\n{result.stdout}{result.stderr}"
    )


_NUMBERED_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


def test_numbered_sql_migrations_have_unique_numbers() -> None:
    """Ensure no migrations/ directory holds two ``NNN_*.sql`` files with the same number.

    The schema_migrations runners record applied migrations by *filename*, so
    two files sharing a number both apply -- but their relative order degrades
    to a lexicographic accident, and the duplicate breaks the "highest number
    is the newest schema" convention operators and reviewers rely on. This is
    exactly what concurrent branches produce (it happened once in the
    connector: two branches each landed an 029), so it is checked repo-wide
    for every directory named ``migrations`` that contains numbered SQL files.
    """
    migration_files_by_dir: dict[Path, list[Path]] = {}
    for sql_file in _get_all_files_with_extension(_REPO_ROOT, ".sql"):
        if sql_file.parent.name == "migrations" and _NUMBERED_MIGRATION_RE.match(sql_file.name):
            migration_files_by_dir.setdefault(sql_file.parent, []).append(sql_file)
    # The check must actually be exercising something; if the discovery ever
    # finds no numbered migrations at all, the glob logic has rotted.
    assert migration_files_by_dir, "No numbered SQL migrations found anywhere; the discovery logic is broken"

    duplicate_descriptions: list[str] = []
    for migrations_dir, files in sorted(migration_files_by_dir.items()):
        # Keyed on the numeric value, not the raw prefix, so a padding mismatch
        # (29_foo.sql vs 029_bar.sql) still counts as the same number.
        files_by_number: dict[int, list[str]] = {}
        for migration_file in files:
            match = _NUMBERED_MIGRATION_RE.match(migration_file.name)
            assert match is not None
            files_by_number.setdefault(int(match.group(1)), []).append(migration_file.name)
        for number, names in sorted(files_by_number.items()):
            if len(names) > 1:
                relative_dir = migrations_dir.relative_to(_REPO_ROOT)
                duplicate_descriptions.append(f"  {relative_dir}: {number} -> {sorted(names)}")
    assert len(duplicate_descriptions) == 0, (
        "Duplicate migration numbers found (renumber the newer file to the next free number):\n"
        + "\n".join(duplicate_descriptions)
    )


def test_prevent_bash_without_strict_mode() -> None:
    """Ensure all bash scripts in the repo use 'set -euo pipefail' for strict error handling.

    The secret-file templates at ``.minds/template/*.sh`` are excluded entirely
    by ``find_bash_scripts_without_strict_mode`` (not merely accommodated in the
    count): they are shell-sourceable env declarations (commented ``export KEY=``
    files consumed by ``scripts/push_vault_from_file.py`` and ``minds-admin env
    deploy`` when seeding HCP Vault / Modal secrets), not executable scripts, so
    ``set -euo pipefail`` is meaningless for them and would only leak strict mode
    into whatever shell sources them.

    The snapshot below is an upper bound on the remaining committed scripts that
    trip the ratchet. Most fall into two groups:

    - Scripts that deliberately use ``set -uo pipefail`` (omitting ``-e``) because
      a single non-zero exit must not abort a best-effort sweep -- e.g.
      ``apps/minds/scripts/mac-runner-reset.sh`` (host cleanup that must not abort
      on one failed step) and ``libs/mngr/imbue/mngr/resources/sigwinch_panes.sh``
      (a per-session tmux repaint that signals every pane's children and must not
      abort when one pane's signal fails).

    - Marker/hook/launch and transcript-library resources under the merged
      ``libs/mngr{,_codex,_opencode,_antigravity}/.../resources/`` plugin ports,
      which routinely omit ``-e`` because they probe for files that may be absent
      and act on non-zero exits, which ``-e`` would abort. Hardening any that do
      not need the exemption is left to those plugins.

    ``find_bash_scripts_without_strict_mode`` counts only tracked ``*.sh`` files
    present on disk, so the effective count can differ between a full local
    checkout and an offload sandbox whose thin-diff image omits some tracked
    paths; the snapshot is pinned to the highest observed count. Adding
    ``sigwinch_panes.sh`` alongside the per-session SIGWINCH client-attached hook
    raised that count from 11 to 12.

    The helper scans the whole git repository containing ``_REPO_ROOT``. When
    this checkout is vendored inside another git repository (e.g. as a subtree
    under ``system/vendor/mngr``), that repository is the outer one, so scope
    the result to scripts under the mngr checkout itself; in a standalone
    checkout the filter is a no-op.
    """
    checkout_root = _REPO_ROOT.resolve()
    violations = [
        v for v in find_bash_scripts_without_strict_mode(_REPO_ROOT) if Path(v).resolve().is_relative_to(checkout_root)
    ]
    assert len(violations) <= snapshot(8), "Bash scripts missing 'set -euo pipefail':\n" + "\n".join(
        f"  - {v}" for v in violations
    )


_PREVENT_OLD_MNG_NAME = RegexRatchetRule(
    rule_name="'mng' (without 'r') occurrences",
    rule_description="The old 'mng' name should not be reintroduced. Use 'mngr' instead.",
    pattern_string=r"mng(?!r)",
)


def test_prevent_old_mng_name_in_file_contents() -> None:
    """Ensure the old 'mng' name (not followed by 'r') is not reintroduced in file contents."""
    exclusions = _SELF_EXCLUSION + BINARY_FILE_EXCLUSION + _DATA_FILE_EXCLUSION + _MIGRATION_SCRIPT_EXCLUSION
    chunks = check_ratchet_rule_all_files(_PREVENT_OLD_MNG_NAME, _REPO_ROOT, exclusions)
    assert len(chunks) <= snapshot(0), _PREVENT_OLD_MNG_NAME.format_failure(chunks)


def test_prevent_old_mng_name_in_file_paths() -> None:
    """Ensure the old 'mng' name (not followed by 'r') is not reintroduced in file paths."""
    mng_not_mngr = re.compile(r"mng(?!r)")
    all_paths = _get_all_files_with_extension(_REPO_ROOT, None)
    mng_paths = [
        p
        for p in all_paths
        if mng_not_mngr.search(str(p.relative_to(_REPO_ROOT)))
        and not any(excl in p.name for excl in _MIGRATION_SCRIPT_EXCLUSION)
    ]
    assert len(mng_paths) <= snapshot(0), (
        f"Found {len(mng_paths)} file paths containing 'mng' (not 'mngr'):\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}" for p in mng_paths)
    )


def test_every_project_has_pypi_readme() -> None:
    """Ensure each project's pyproject.toml has a readme field pointing to an existing file.

    Every published package should have a README so that PyPI displays useful
    information. This checks two things:
    1. The [project] section contains a `readme` key
    2. The referenced file exists on disk
    """
    missing_field: list[str] = []
    missing_file: list[str] = []

    for project_dir in _get_all_project_dirs():
        pyproject_path = project_dir / "pyproject.toml"
        pyproject = tomlkit.parse(pyproject_path.read_text())
        project_section = pyproject.get("project", {})

        readme_value = project_section.get("readme")
        if not isinstance(readme_value, str):
            missing_field.append(project_dir.name)
            continue

        if not (project_dir / readme_value).exists():
            missing_file.append(f"{project_dir.name} (references {readme_value})")

    errors: list[str] = []
    if missing_field:
        errors.append("Missing readme field in [project]: " + ", ".join(missing_field))
    if missing_file:
        errors.append("readme file does not exist: " + ", ".join(missing_file))

    assert len(errors) == 0, "Projects with PyPI readme issues:\n" + "\n".join(f"  - {e}" for e in errors)


def _is_mngr_plugin(project_dir: Path) -> bool:
    """Return True if the project registers itself as an mngr plugin.

    An mngr plugin is any project whose ``pyproject.toml`` declares a
    ``[project.entry-points.mngr]`` table -- that entry point group is how mngr's
    pluggy-based plugin manager discovers and loads a package's hooks at runtime.
    Support libraries that merely have an ``mngr_`` name prefix but register no
    such entry point (e.g. ``mngr_mapreduce``, ``mngr_vps_docker``) are *not*
    plugins and are intentionally excluded.
    """
    pyproject = tomlkit.parse((project_dir / "pyproject.toml").read_text())
    entry_points = pyproject.get("project", {}).get("entry-points", {})
    return "mngr" in entry_points


def _conftest_registers_plugin_test_fixtures(conftest_path: Path) -> bool:
    """Return True if the conftest calls ``register_plugin_test_fixtures(...)``.

    Parses the AST (rather than substring-matching) so that comments or
    docstrings mentioning the helper do not count -- only an actual call does.
    """
    tree = ast.parse(conftest_path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name == "register_plugin_test_fixtures":
            return True
    return False


def test_every_mngr_plugin_isolates_home_in_tests() -> None:
    """Ensure each mngr plugin pulls in mngr's shared test fixtures.

    Every mngr plugin (a project with a ``[project.entry-points.mngr]`` table)
    must have a ``conftest.py`` that calls
    ``register_plugin_test_fixtures(globals())`` from
    ``imbue.mngr.utils.plugin_testing``. That helper injects the shared fixture
    set -- crucially the autouse ``setup_test_mngr_env`` fixture, which redirects
    ``HOME`` to a temp dir so the plugin's tests cannot read or write the real
    ``~/.mngr`` / ``~/.claude.json``.

    Without it, a plugin run on its own (``pytest libs/<plugin>``) does *not*
    inherit that autouse fixture -- mngr's root conftest is not an ancestor of
    the plugin's test files -- and the tests execute against the developer's real
    home directory. This is the meta-level analogue of
    ``test_every_project_has_pypi_readme``: a symmetric requirement that every
    plugin opt into the shared HOME-isolation infrastructure the same way.

    The single sanctioned mechanism is ``register_plugin_test_fixtures``; the
    older ``pytest_plugins = ["imbue.mngr.conftest"]`` form is intentionally not
    accepted here so the codebase keeps exactly one way to do this.
    """
    missing: list[str] = []
    for project_dir in _get_all_project_dirs():
        if not _is_mngr_plugin(project_dir):
            continue
        conftests = list(project_dir.rglob("conftest.py"))
        if not any(_conftest_registers_plugin_test_fixtures(c) for c in conftests):
            missing.append(project_dir.name)

    assert len(missing) == 0, (
        "Every mngr plugin must isolate HOME in its tests by calling "
        "register_plugin_test_fixtures(globals()) (from imbue.mngr.utils.plugin_testing) "
        "in a conftest.py. Add it to the plugin's project-level conftest.py, e.g.:\n\n"
        "    from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures\n\n"
        "    register_plugin_test_fixtures(globals())\n\n"
        "Plugins missing it (tests would run against the real ~/.mngr / ~/.claude.json):\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


_REQUIRED_WHEEL_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "*_test.py",
    "test_*.py",
    "**/conftest.py",
    "**/testing.py",
)


def test_every_project_excludes_tests_from_wheel() -> None:
    """Ensure each project's wheel build excludes test code from the published artifact.

    Without this, hatchling bundles `_test.py`, `conftest.py`, and `testing.py`
    helpers into the wheel, so any consumer that pip-installs the package ships our
    test code in their `site-packages/`.

    Each project's `[tool.hatch.build.targets.wheel].exclude` must literally contain all
    of `*_test.py`, `test_*.py`, `**/conftest.py`, and `**/testing.py`. The patterns are
    required uniformly even for projects that do not currently have a matching file --
    that way, adding a new `testing.py` (or similar) tomorrow needs no second PR.

    Projects with `only-include` (an explicit whitelist) are exempt.
    """
    missing: list[str] = []
    for project_dir in _get_all_project_dirs():
        pyproject = tomlkit.parse((project_dir / "pyproject.toml").read_text())
        wheel = pyproject.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
        if "only-include" in wheel:
            continue
        exclude_patterns = [str(x) for x in wheel.get("exclude", [])]
        absent = [pat for pat in _REQUIRED_WHEEL_EXCLUDE_PATTERNS if pat not in exclude_patterns]
        if absent:
            missing.append(f"{project_dir.name} (missing: {absent})")

    assert len(missing) == 0, (
        "Projects must exclude test files from their wheel build. Add to "
        "[tool.hatch.build.targets.wheel]:\n"
        '    exclude = ["*_test.py", "test_*.py", "**/conftest.py", "**/testing.py"]\n\n'
        "Offending projects:\n" + "\n".join(f"  - {m}" for m in missing)
    )


def _has_test_files(project_dir: Path) -> bool:
    """Return True if the project contains any test files.

    Stops at the first match rather than materializing every one: rglob
    descends into gitignored subtrees (apps/minds carries node_modules and the
    frontend build), and enumerating one of those in full costs more than every
    other project put together -- enough to blow a caller's timeout on a slow
    sandbox.
    """
    for pattern in ["*_test.py", "test_*.py"]:
        if next(project_dir.rglob(pattern), None) is not None:
            return True
    return False


def _tracked_present_files() -> list[str]:
    """Return git-tracked paths that exist in the working tree.

    Files that are deleted in the working tree are excluded: they are on their
    way out of the repo, and offload sandboxes reconstruct branch state as a
    base commit plus an unstaged diff, which leaves files that a commit deletes
    in `git ls-files` output even though they are absent.
    """
    tracked = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO_ROOT,
    )
    return [line for line in tracked.stdout.splitlines() if line.strip() and (_REPO_ROOT / line).exists()]


def _find_tracked_gitignored_files() -> list[str]:
    """Return tracked, working-tree-present files that match .gitignore patterns."""
    present = _tracked_present_files()
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(present) + "\n",
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    return [line for line in ignored.stdout.splitlines() if line.strip()]


def test_no_gitignored_files_are_tracked() -> None:
    """Ensure no tracked files match .gitignore patterns.

    Files that are gitignored should not be committed. If they were committed
    accidentally, remove them with `git rm --cached <path>`.
    """
    offending = _find_tracked_gitignored_files()
    assert len(offending) == 0, (
        "The following tracked files match .gitignore patterns (remove with `git rm --cached`):\n"
        + "\n".join(f"  - {f}" for f in offending)
    )


def test_gitignore_patterns_use_double_star() -> None:
    """Ensure every active .gitignore pattern starts with **/ or contains a path separator.

    All patterns must use **/ so they are directly compatible with .dockerignore
    syntax (where bare names only match at root). Patterns with an interior /
    (like */*/_tasks/) are already path-qualified and are allowed.

    .dockerignore is generated from .gitignore by the _generate-dockerignore
    justfile recipe before each offload run, so the two files must use patterns
    valid in both syntaxes. Enforcing **/ on the .gitignore side keeps the
    generator close to a plain passthrough -- its only semantic patch-up is
    re-appending the .minds/template negations in the docker-honored form
    (see test_generated_dockerignore_ships_all_committed_files).
    """
    gitignore = (_REPO_ROOT / ".gitignore").read_text()
    violations: list[str] = []
    for lineno, line in enumerate(gitignore.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern = stripped.lstrip("!")
        if pattern.startswith("**/"):
            continue
        # Contains a / before the last char (e.g. */*/_tasks/)
        core = pattern.rstrip("/")
        if "/" in core:
            continue
        violations.append(f"  line {lineno}: {stripped}")
    assert len(violations) == 0, (
        "The following .gitignore patterns need a **/ prefix.\n"
        "This keeps .gitignore directly compatible with .dockerignore:\n" + "\n".join(violations)
    )


_GENERATE_DOCKERIGNORE_SCRIPT = "scripts/generate_dockerignore.sh"


def _generated_dockerignore_patterns(tmp_path: Path) -> list[str]:
    """Run the real generation script into ``tmp_path`` and return its patterns.

    Also asserts the _generate-dockerignore recipe (private.just) still routes
    through the script, so what this test evaluates is what offload uses.
    """
    recipe_text = (_REPO_ROOT / "private.just").read_text()
    assert f"bash {_GENERATE_DOCKERIGNORE_SCRIPT}" in recipe_text, (
        f"private.just's _generate-dockerignore recipe no longer invokes {_GENERATE_DOCKERIGNORE_SCRIPT}; "
        "update test_generated_dockerignore_ships_all_committed_files to exercise whatever replaced it."
    )
    output_path = tmp_path / "dockerignore.generated"
    # Streams are inherited (not captured) so that if the script fails, its
    # stderr lands in pytest's captured output next to the CalledProcessError.
    subprocess.run(
        ["bash", _GENERATE_DOCKERIGNORE_SCRIPT, str(output_path)],
        cwd=_REPO_ROOT,
        check=True,
    )
    return output_path.read_text().splitlines()


@pytest.mark.skipif(not _IS_SOURCE_OF_TRUTH, reason="the _generate-dockerignore recipe is absent on the public mirror")
def test_generated_dockerignore_ships_all_committed_files(tmp_path: Path) -> None:
    """No git-committed file may be excluded by the generated .dockerignore.

    Offload builds its sandbox images from the .dockerignore that
    scripts/generate_dockerignore.sh derives from .gitignore, and Modal
    evaluates it with its docker-style matcher. That matcher does not
    honor .gitignore's anchored `!/...` negation form, so a committed file can
    silently vanish from sandbox images even though git tracks it (this
    happened to the committed .minds/template schemas). Run the real script
    and evaluate its output with Modal's real matcher against every committed
    path to catch any such exclusion at test time instead of as a missing
    file in CI.
    """
    file_pattern_matcher = pytest.importorskip("modal.file_pattern_matcher")
    # Blank and `#`-comment lines carry no patterns in dockerignore syntax.
    patterns = [
        pattern.strip()
        for pattern in _generated_dockerignore_patterns(tmp_path)
        if pattern.strip() and not pattern.strip().startswith("#")
    ]
    matcher = file_pattern_matcher.FilePatternMatcher(*patterns)
    excluded = [path for path in _tracked_present_files() if matcher(Path(path))]
    assert len(excluded) == 0, (
        "The following committed files would be excluded from offload sandbox images by the\n"
        "generated .dockerignore (see scripts/generate_dockerignore.sh):\n"
        + "\n".join(f"  - {path}" for path in excluded)
    )


def test_every_project_with_tests_has_coverage_config() -> None:
    """Ensure each project with tests has pytest coverage configuration in its pyproject.toml.

    Every project that contains test files must have:
    1. A [tool.pytest.ini_options] section with a --cov flag scoped to the project's package
    2. A [tool.coverage.run] section with omit patterns for test files
    """
    missing_pytest: list[str] = []
    missing_cov_flag: list[str] = []
    missing_coverage_run: list[str] = []

    for project_dir in _get_all_project_dirs():
        if not _has_test_files(project_dir):
            continue

        pyproject_path = project_dir / "pyproject.toml"
        pyproject = tomlkit.parse(pyproject_path.read_text())

        tool = pyproject.get("tool", {})

        # Check for [tool.pytest.ini_options]
        pytest_opts = tool.get("pytest", {}).get("ini_options", {})
        if not pytest_opts:
            missing_pytest.append(project_dir.name)
            continue

        # Check that addopts contains a --cov flag
        addopts = pytest_opts.get("addopts", [])
        has_cov_flag = any(str(opt).startswith("--cov=") for opt in addopts)
        if not has_cov_flag:
            missing_cov_flag.append(project_dir.name)

        # Check for [tool.coverage.run]
        coverage_run = tool.get("coverage", {}).get("run", {})
        if not coverage_run:
            missing_coverage_run.append(project_dir.name)

    errors: list[str] = []
    if missing_pytest:
        errors.append("Missing [tool.pytest.ini_options]: " + ", ".join(missing_pytest))
    if missing_cov_flag:
        errors.append("Missing --cov= in addopts: " + ", ".join(missing_cov_flag))
    if missing_coverage_run:
        errors.append("Missing [tool.coverage.run]: " + ", ".join(missing_coverage_run))

    assert len(errors) == 0, "Projects with tests are missing coverage configuration:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


# --- Meta: ensure every project has the changelog layout files ---


@pytest.mark.skipif(not _IS_SOURCE_OF_TRUTH, reason="the synthetic dev project is absent on the public mirror")
def test_every_project_has_changelog_layout() -> None:
    """Ensure every project (libs/<name>, apps/<name>, and the synthetic dev)
    has the full changelog layout: ``CHANGELOG.md``, ``UNABRIDGED_CHANGELOG.md``,
    and a ``changelog/.gitkeep`` anchoring the directory for per-PR entries.

    Mirrors ``test_every_project_has_test_ratchets_file`` and
    ``test_every_project_has_pypi_readme``: a symmetric requirement that
    every project participates in the consolidation flow uniformly.
    """
    missing: list[str] = []
    for project in all_known_projects(_REPO_ROOT):
        proj_dir = get_project_dir(project, _REPO_ROOT)
        required = [
            proj_dir / "CHANGELOG.md",
            proj_dir / "UNABRIDGED_CHANGELOG.md",
            project_entries_dir(project, _REPO_ROOT) / ".gitkeep",
        ]
        for target in required:
            if not target.exists():
                missing.append(str(target.relative_to(_REPO_ROOT)))

    assert not missing, (
        "The following projects are missing required changelog-layout files:\n"
        + "\n".join(f"  - {m}" for m in missing)
        + "\n\nEvery project must have CHANGELOG.md (with an '## [Unreleased]' heading), "
        "UNABRIDGED_CHANGELOG.md, and a changelog/ directory containing a .gitkeep."
    )


# Regex matching top-level omit patterns that fully exclude a subproject's package,
# e.g. "libs/mngr_modal/imbue/mngr_modal/*" -> package "mngr_modal".
_FULLY_OMITTED_PACKAGE_PATTERN = re.compile(r"^(?:libs|apps)/([^/]+)/imbue/\1/\*$")


def _get_cov_packages(addopts: object) -> frozenset[str]:
    """Extract the X in every `--cov=X` entry from a pytest addopts list."""
    if not isinstance(addopts, list):
        return frozenset()
    return frozenset(str(opt).removeprefix("--cov=") for opt in addopts if str(opt).startswith("--cov="))


def _get_addopts(pyproject: dict) -> object:
    return pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", [])


def _get_coverage_omit(pyproject: dict) -> list[str]:
    return [str(x) for x in pyproject.get("tool", {}).get("coverage", {}).get("run", {}).get("omit", [])]


def test_top_level_cov_flags_are_union_of_subproject_cov_flags() -> None:
    """Ensure the top-level pyproject.toml `--cov=` flags are exactly the union of the
    subprojects' `--cov=` flags, except for packages whose source is fully omitted in the
    top-level `[tool.coverage.run].omit` (e.g. `libs/mngr_modal/imbue/mngr_modal/*`).

    Standalone (non-workspace-member) projects are out of scope: the root run cannot
    import them at all, so a root ``--cov=`` flag for one would only ever warn that the
    module was never imported. They own their coverage in their own project.

    Keeps the root coverage scope in sync with the per-project scopes so a new subproject
    cannot silently drop out of combined coverage collection.
    """
    top_pyproject = tomlkit.parse((_REPO_ROOT / "pyproject.toml").read_text())
    top_cov = _get_cov_packages(_get_addopts(top_pyproject))
    top_omit = _get_coverage_omit(top_pyproject)
    fully_omitted = frozenset(
        f"imbue.{m.group(1)}" for pat in top_omit if (m := _FULLY_OMITTED_PACKAGE_PATTERN.match(pat)) is not None
    )

    subproject_cov: set[str] = set()
    for project_dir in _get_workspace_project_dirs():
        pyproject = tomlkit.parse((project_dir / "pyproject.toml").read_text())
        # Only consider --cov= flags that target the `imbue.<pkg>` namespace;
        # the top-level pyproject.toml only exposes that shape via its `source =
        # ["imbue"]`, so flat-layout projects (e.g. apps/modal_litellm with a
        # bare `app.py` and `--cov=app`) cannot be expressed at the root and
        # must own their own coverage in isolation.
        for cov in _get_cov_packages(_get_addopts(pyproject)):
            if cov.startswith("imbue."):
                subproject_cov.add(cov)

    expected_top_cov = subproject_cov - fully_omitted
    missing = expected_top_cov - top_cov
    extra = top_cov - expected_top_cov

    errors: list[str] = []
    if missing:
        errors.append(
            "Subprojects declare --cov= flags that are missing from the top-level pyproject.toml "
            "(add them to [tool.pytest.ini_options].addopts, or fully omit the package in "
            "[tool.coverage.run].omit):\n" + "\n".join(f"    --cov={m}" for m in sorted(missing))
        )
    if extra:
        errors.append(
            "Top-level pyproject.toml has --cov= flags that no subproject declares:\n"
            + "\n".join(f"    --cov={e}" for e in sorted(extra))
        )

    assert len(errors) == 0, "Top-level --cov= flags out of sync with subprojects:\n" + "\n".join(errors)


def test_standalone_project_ci_gates_list_every_in_repo_dependency() -> None:
    """A standalone project's CI job must be gated on all of its in-repo dependencies.

    A standalone project is invisible to the offload run, so one path-gated job is the
    only thing that exercises it. It resolves its in-repo dependencies as editable path
    sources, which means a change to any of them lands in that project's venv without
    touching the project directory -- and a gate that does not list the dependency
    simply does not run, reporting green for a change it never built. The lock is the
    authority on what those dependencies are, so the gate is checked against it rather
    than against a second hand-written list.
    """
    workflow = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    missing: list[str] = []
    for project_dir in _get_standalone_project_dirs():
        rel_project = project_dir.relative_to(_REPO_ROOT)
        lock_text = (project_dir / "uv.lock").read_text()
        # `source = { editable = "../../libs/foo" }` -- the project's own entry is "."
        editable_deps = {
            (project_dir / raw).resolve().relative_to(_REPO_ROOT.resolve())
            for raw in re.findall(r'source = \{ editable = "([^"]+)" \}', lock_text)
            if raw != "."
        }
        gate = _find_ci_path_gate(workflow, rel_project)
        assert gate is not None, f"no path-gated CI job found for standalone project {rel_project}"
        # Compare whole shell words, not substrings: `libs/mngr` is a substring of the
        # listed `libs/mngr_usage`, so a substring test would report a gate that omits
        # `libs/mngr` as complete.
        gate_words = set(gate.split())
        missing.extend(
            f"{rel_project}: CI gate omits {dep}" for dep in sorted(editable_deps) if str(dep) not in gate_words
        )
    assert not missing, (
        "Standalone projects' CI path gates must list every in-repo editable dependency in their "
        "uv.lock:\n" + "\n".join(f"  - {m}" for m in missing)
    )


def _find_ci_path_gate(workflow: dict, project_dir: Path) -> str | None:
    """The shell body of the `git diff --name-only` step that gates ``project_dir``'s CI job.

    Returned with backslash-newline continuations collapsed to spaces, so the gate's
    path arguments -- one per continued line -- are plain whitespace-delimited words
    that callers can match exactly.
    """
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            run = step.get("run", "").replace("\\\n", " ")
            if "git diff --name-only" in run and f" {project_dir} " in run:
                return run
    return None


# ty logs this instead of a diagnostic when every path it was handed is excluded.
# It is the only thing that distinguishes "checked, and clean" from "not checked at
# all", both of which otherwise print "All checks passed!" and exit 0.
_TY_NO_FILES_FOUND = "No python files found under the given path(s)"


def _run_root_ty_probe(path: Path, is_force_exclude_enabled: bool) -> str:
    """Run the root workspace's ty over a single path and return everything it printed.

    ty ignores `[tool.ty.src].exclude` for paths named on the command line unless
    `--force-exclude` is passed, so this asks ty's own matcher whether the path is
    covered instead of reimplementing its gitignore-style glob semantics. Passing
    ``is_force_exclude_enabled=False`` asks the complementary question: whether ty
    finds the file at all once the exclude list no longer applies to it.
    """
    command = ["uv", "run", "ty", "check", "--output-format=concise"]
    if is_force_exclude_enabled:
        command.append("--force-exclude")
    result = subprocess.run([*command, str(path)], cwd=_REPO_ROOT, capture_output=True, text=True)
    # 0 is a clean check and 1 is diagnostics found; anything else is ty never getting
    # as far as reading the file, whose output the caller would otherwise read as
    # "this path was checked".
    assert result.returncode in (0, 1), f"ty exited {result.returncode} for {path}:\n{result.stdout}{result.stderr}"
    return result.stdout + result.stderr


def _get_root_ty_excluded_python_files() -> tuple[Path, ...]:
    """Return the Python files the root's own ``[tool.ty.src].exclude`` names.

    Entries that are globs, or that no longer resolve on disk, are skipped: this is a
    source of paths ty is known to exclude, not an audit of the list.
    """
    root_exclude = tomlkit.parse((_REPO_ROOT / "pyproject.toml").read_text())["tool"]["ty"]["src"]["exclude"]
    excluded_files: list[Path] = []
    for entry in root_exclude:
        if "*" in str(entry):
            continue
        path = _REPO_ROOT / str(entry)
        if path.is_dir():
            excluded_files.extend(_get_all_files_with_extension(path, ".py"))
        elif path.is_file() and path.suffix == ".py":
            excluded_files.append(path)
    return tuple(excluded_files)


def _assert_root_ty_probe_can_see_exclusions(paths_that_must_stay_checked: frozenset[Path]) -> None:
    """Fail unless the probe can still tell an excluded path from a checked one.

    "Not checked" reaches the probe as a log line rather than an exit code, so a ty
    release that reworded or dropped that line would silently turn every clean probe
    result into a vacuous pass. The control is a Python file the root's own
    `[tool.ty.src].exclude` names, probed twice: without `--force-exclude` ty must
    find it, with `--force-exclude` ty must not. That covers both halves of what
    callers rely on -- the marker still means what they read it to mean, and ty still
    applies its *configured* exclude list to a path named on the command line. A
    control excluded by `--exclude` on the command line would show only the first
    half, and stay green through a ty release that stopped honouring the config.

    ``paths_that_must_stay_checked`` are paths the caller is about to assert ty does
    check; none of them can double as the control, since the two assertions would
    then contradict each other and the caller's failure is the one worth reading.
    """
    candidates = [path for path in _get_root_ty_excluded_python_files() if path not in paths_that_must_stay_checked]
    assert candidates, (
        "no entry in the root [tool.ty.src] exclude resolves to a Python file usable as a control, "
        "so there is no way left to show that ty still reports an excluded path as unchecked. Add a "
        "non-glob entry naming a path that exists, or teach _get_root_ty_excluded_python_files to "
        "expand glob entries."
    )
    control = min(candidates)

    unforced_output = _run_root_ty_probe(control, is_force_exclude_enabled=False)
    assert _TY_NO_FILES_FOUND not in unforced_output, (
        f"{control.relative_to(_REPO_ROOT)} was picked as a control because the root "
        "[tool.ty.src] excludes it, but ty does not find it even with the exclude list switched "
        f"off, so it proves nothing about exclusion:\n{unforced_output}"
    )

    forced_output = _run_root_ty_probe(control, is_force_exclude_enabled=True)
    assert _TY_NO_FILES_FOUND in forced_output, (
        f"cannot tell whether ty checked a path: the root [tool.ty.src] excludes "
        f"{control.relative_to(_REPO_ROOT)}, yet probing it did not produce {_TY_NO_FILES_FOUND!r}. "
        "Either ty stopped applying its configured excludes under --force-exclude or it reworded "
        f"that line; either way this check is now blind:\n{forced_output}"
    )


@pytest.mark.timeout(120)
def test_standalone_project_ty_carve_outs_are_checked_by_the_root_workspace() -> None:
    """Whatever a standalone project excludes from its own type check must be checked here.

    A standalone project excludes a path when its own venv cannot resolve that path's
    imports -- typically because the file is shipped elsewhere and runs against the
    monorepo venv, which is this workspace. That makes the root the only place left
    that can check it, and nothing fails if the root stops: the file is type-checked
    nowhere while both projects stay green. Each side's exclude reads as reasonable on
    its own; only the pair is wrong, so only a check that spans both can see it.

    This has to live at the repo root rather than in the standalone project, because
    the project's CI job is path-gated on the project's own directory and its in-repo
    dependencies -- an edit to the root exclude alone would never run it.
    """
    carve_out_files: list[Path] = []
    unprobeable: list[str] = []
    for project_dir in _get_standalone_project_dirs():
        tool_config = tomlkit.parse((project_dir / "pyproject.toml").read_text()).get("tool", {})
        for entry in tool_config.get("ty", {}).get("src", {}).get("exclude", []):
            # An entry has to name a path that can be handed straight back to ty. A
            # glob would have to be expanded first, and guessing at how ty expands it
            # is the reimplementation this check exists to avoid. A carve-out this
            # check cannot probe is a carve-out it cannot guard, so it is reported.
            carve_out = project_dir / str(entry)
            if carve_out.is_dir():
                carve_out_files.extend(_get_all_files_with_extension(carve_out, ".py"))
            elif carve_out.is_file():
                # A non-Python file is not something either type check would read.
                if carve_out.suffix == ".py":
                    carve_out_files.append(carve_out)
            else:
                unprobeable.append(f"{project_dir.relative_to(_REPO_ROOT)}: {entry}")

    assert not unprobeable, (
        "these [tool.ty.src] exclude entries do not name an existing directory or file, so this "
        "check cannot hand them to ty and cannot tell whether the root workspace still covers "
        "them. Respell each as a path, or teach this check to expand the pattern:\n"
        + "\n".join(f"  - {entry}" for entry in unprobeable)
    )
    assert carve_out_files, (
        "no standalone project excludes a path from its own [tool.ty.src] any more; "
        "this check has nothing left to guard and should be deleted with the last carve-out"
    )

    _assert_root_ty_probe_can_see_exclusions(frozenset(carve_out_files))

    unchecked = [
        f for f in carve_out_files if _TY_NO_FILES_FOUND in _run_root_ty_probe(f, is_force_exclude_enabled=True)
    ]
    assert not unchecked, (
        "the root [tool.ty.src] exclude covers files that their own standalone project also "
        "excludes, so they are type-checked nowhere:\n"
        + "\n".join(f"  - {f.relative_to(_REPO_ROOT)}" for f in sorted(unchecked))
    )


@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_top_level_coverage_omit_covers_subproject_omits() -> None:
    """For every file in a subproject's package tree that the subproject's
    `[tool.coverage.run].omit` patterns exclude, the top-level
    `[tool.coverage.run].omit` must also exclude it.

    Walks every workspace package tree: well under a second locally, but under
    offload's sandbox I/O contention the walk has hit the default 10s
    pytest-timeout, so it gets the same budget and retry as the other
    repo-wide tree walks here.

    Checks the file-level semantic (not pattern-level equality) because root and
    subproject pyproject.tomls use different path conventions: subprojects use globs
    like `*/testing.py`, while root can use either globs or fully-qualified paths like
    `libs/<pkg>/imbue/<pkg>/testing.py`. Walking concrete files and matching via
    fnmatch (the same matcher coverage.py uses) makes both forms equivalent.

    Standalone (non-workspace-member) projects are out of scope: the root run never
    measures them, so the root omit list has nothing to say about their files.

    Prevents a new subproject from silently omitting files that combined coverage
    still counts at the root.
    """
    top_omit = _get_coverage_omit(tomlkit.parse((_REPO_ROOT / "pyproject.toml").read_text()))

    def root_excludes(rel_repo_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_repo_path, pat) for pat in top_omit)

    missing: dict[str, list[str]] = {}
    for project_dir in _get_workspace_project_dirs():
        pkg_root = project_dir / "imbue" / project_dir.name
        if not pkg_root.exists():
            continue
        sub_patterns = _get_coverage_omit(tomlkit.parse((project_dir / "pyproject.toml").read_text()))
        if not sub_patterns:
            continue
        for f in pkg_root.rglob("*.py"):
            if not f.is_file():
                continue
            rel_subproject = str(f.relative_to(project_dir))
            if not any(fnmatch.fnmatch(rel_subproject, pat) for pat in sub_patterns):
                continue
            rel_repo = str(f.relative_to(_REPO_ROOT))
            if not root_excludes(rel_repo):
                missing.setdefault(project_dir.name, []).append(rel_repo)

    errors = [
        f"  {proj}:\n" + "\n".join(f"    - {p}" for p in sorted(files)) for proj, files in sorted(missing.items())
    ]
    assert len(errors) == 0, (
        "Top-level [tool.coverage.run].omit is missing entries for files that subprojects omit:\n" + "\n".join(errors)
    )


# --- Meta: offload CI config performance invariants ---


@pytest.mark.skipif(not _IS_SOURCE_OF_TRUTH, reason="offload configs are absent on the public mirror")
def test_offload_configs_suppress_per_batch_coverage_reports() -> None:
    """Guard the offload CI coverage invariants established in MIND-142.

    Per-run coverage reports walk every measured file and are never consumed
    from sandboxes (CI combines the raw .coverage data files on the runner and
    enforces the gates there), so offload batches suppress them:

    - Root addopts must not contain ``--cov-report*`` or ``--coverage-to-file``.
      Keeping them out of addopts is also what allows ``--cov-report=`` to
      clear reports entirely -- pytest-cov only treats the empty value as
      "no reports" when it is the sole ``--cov-report`` given.
    - offload-modal.toml must pass ``--cov-report=`` so its batches generate
      no reports but still write the .coverage data file the gates combine.

    Two discovery-side speedups were prototyped in MIND-142; they diverge now:

    - Invoking ``.venv/bin/pytest`` directly instead of ``uv run pytest`` landed
      upstream -- offload activates the project virtualenv itself as of 0.9.11 --
      so it must NOT be re-added as config here.
    - Skipping coverage tracing during ``pytest --collect-only`` (``--no-cov``)
      lives in each config's framework ``discovery_args``. offload exposes the
      knob but does not auto-inject discovery args -- by design they stay
      explicit -- so every offload config sets it.
    """
    errors: list[str] = []

    root_addopts = _get_addopts(tomlkit.parse((_REPO_ROOT / "pyproject.toml").read_text()))
    if isinstance(root_addopts, list):
        for opt in root_addopts:
            if str(opt).startswith("--cov-report") or str(opt) == "--coverage-to-file":
                errors.append(f"root pyproject.toml addopts must not contain {opt} (see MIND-142 note in addopts)")

    modal_config = tomlkit.parse((_REPO_ROOT / "offload-modal.toml").read_text())
    run_args = str(modal_config.get("framework", {}).get("run_args", ""))
    if "--cov-report=" not in run_args.split():
        errors.append(
            "offload-modal.toml: framework.run_args must contain `--cov-report=` to suppress per-batch reports"
        )

    for config_name in (
        "offload-modal.toml",
        "offload-modal-acceptance.toml",
        "offload-modal-release.toml",
        "offload-modal-minds-snapshot.toml",
    ):
        discovery_args = str(
            tomlkit.parse((_REPO_ROOT / config_name).read_text()).get("framework", {}).get("discovery_args", "")
        )
        if "--no-cov" not in discovery_args.split():
            errors.append(
                f"{config_name}: framework.discovery_args must contain `--no-cov` to skip coverage tracing "
                "during discovery (offload does not auto-inject discovery args -- by design they stay explicit)"
            )

    assert len(errors) == 0, "offload CI coverage invariants violated:\n" + "\n".join(f"  - {e}" for e in errors)


@pytest.mark.skipif(not _IS_SOURCE_OF_TRUTH, reason="offload CI action is absent on the public mirror")
def test_offload_version_pinned_consistently() -> None:
    """The offload version is pinned in exactly two places; they must match.

    - ``.github/actions/setup-offload/action.yml`` -- the composite action every
      offload CI job uses to install the orchestrator binary.
    - ``libs/mngr/imbue/mngr/resources/Dockerfile`` -- ``OFFLOAD_VERSION``, the
      in-image ``offload apply-diff`` binary.

    The orchestrator and the in-sandbox binary must be the same version, so a
    bump has to touch both. This test fails if they drift apart.
    """
    version_pattern = r"([0-9]+(?:\.[0-9]+)+)"

    action_text = (_REPO_ROOT / ".github/actions/setup-offload/action.yml").read_text()
    action_match = re.search(rf'default:\s*"{version_pattern}"', action_text)
    assert action_match is not None, "could not find the offload version default in setup-offload/action.yml"

    dockerfile_text = (_REPO_ROOT / "libs/mngr/imbue/mngr/resources/Dockerfile").read_text()
    dockerfile_match = re.search(rf"OFFLOAD_VERSION={version_pattern}", dockerfile_text)
    assert dockerfile_match is not None, "could not find OFFLOAD_VERSION in the mngr Dockerfile"

    assert action_match.group(1) == dockerfile_match.group(1), (
        f"offload version mismatch: the setup-offload composite action pins {action_match.group(1)} "
        f"but the mngr Dockerfile pins {dockerfile_match.group(1)}. Bump both together."
    )


@cache
def _collect_class_defs_for_model_config_checks() -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """Collect, repo-wide, each class's base names and any extra="forbid" declarations in its body.

    Cached: three tests share this repo-wide AST walk, and the meta-ratchet
    xdist group runs them in one process, so the repo is parsed once instead
    of three times (the walk alone can approach a 10s timeout on a loaded CI
    sandbox).

    Returns ``(base_names_by_class, forbid_locations_by_class)``. Classes are keyed
    by bare name; two same-named classes in different files have their bases merged,
    which can only over-approximate a base's subclass set (acceptable for guards
    that should match nothing). Cached (callers only read the result): the
    repo-wide AST parse is the dominant cost of its three consumer tests, and
    without the cache each one re-parses every .py file in the repo.
    """
    base_names_by_class: dict[str, set[str]] = {}
    forbid_locations_by_class: dict[str, list[str]] = {}
    for py_path in _get_all_files_with_extension(_REPO_ROOT, ".py"):
        try:
            tree = ast.parse(py_path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                base.id if isinstance(base, ast.Name) else base.attr
                for base in node.bases
                if isinstance(base, (ast.Name, ast.Attribute))
            }
            base_names_by_class.setdefault(node.name, set()).update(base_names)
            if _class_body_sets_extra_forbid(node):
                forbid_locations_by_class.setdefault(node.name, []).append(
                    f"{py_path.relative_to(_REPO_ROOT)}:{node.lineno}"
                )
    return base_names_by_class, forbid_locations_by_class


def _transitive_subclass_names(base_names_by_class: dict[str, set[str]], seed_names: set[str]) -> set[str]:
    """Every class name that (transitively, by bare name) inherits one of ``seed_names``, seeds included."""
    subclass_names = set(seed_names)
    is_growing = True
    while is_growing:
        newly_found = {
            class_name
            for class_name, base_names in base_names_by_class.items()
            if class_name not in subclass_names and base_names & subclass_names
        }
        is_growing = bool(newly_found)
        subclass_names.update(newly_found)
    return subclass_names


def _class_body_sets_extra_forbid(class_def: ast.ClassDef) -> bool:
    """Whether the class body assigns a model_config containing extra="forbid".

    Handles all three spellings: ``model_config = ConfigDict(extra="forbid")``,
    the plain-dict form ``model_config = {"extra": "forbid"}``, and the annotated
    form ``model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")``.
    """
    for statement in class_def.body:
        if isinstance(statement, ast.Assign):
            if not any(isinstance(t, ast.Name) and t.id == "model_config" for t in statement.targets):
                continue
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            if not (isinstance(statement.target, ast.Name) and statement.target.id == "model_config"):
                continue
            if statement.value is None:
                continue
            value = statement.value
        else:
            continue
        if _config_value_sets_extra_forbid(value):
            return True
    return False


def _config_value_sets_extra_forbid(value: ast.expr) -> bool:
    """Whether a model_config value expression contains extra="forbid"."""
    if isinstance(value, ast.Call):
        for keyword in value.keywords:
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Constant):
                if keyword.value.value == "forbid":
                    return True
    elif isinstance(value, ast.Dict):
        for key, dict_value in zip(value.keys, value.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "extra":
                if isinstance(dict_value, ast.Constant) and dict_value.value == "forbid":
                    return True
    else:
        pass
    return False


# Repo-wide AST walk (cached, but the first caller pays it); the default 10s
# timeout is too tight on a loaded CI sandbox.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_event_envelope_subclasses_never_re_forbid_extra() -> None:
    """No EventEnvelope subclass anywhere in the repo may set extra="forbid".

    EventEnvelope models are persisted, cross-process, cross-version event
    records (events.jsonl streams). The base class deliberately ignores unknown
    fields so that an additive schema change never makes an already-released
    reader reject a shared append-only log -- the downgrade wedge of
    mngr-internal#422. A subclass re-tightening ``extra`` to ``"forbid"``
    silently reintroduces that wedge for its stream, so it is banned outright.
    Subclass membership is computed transitively by class name across the whole
    repo (an over-approximation, which for this guard can only catch more).
    """
    base_names_by_class, forbid_locations_by_class = _collect_class_defs_for_model_config_checks()
    envelope_class_names = _transitive_subclass_names(base_names_by_class, {"EventEnvelope"})

    violations = [
        f"{class_name} at {location}"
        for class_name in sorted(envelope_class_names)
        for location in forbid_locations_by_class.get(class_name, [])
    ]
    assert len(violations) == 0, (
        'EventEnvelope subclasses must not set extra="forbid" (persisted event records must tolerate '
        "additive fields from other program versions; see mngr-internal#422):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# Repo-wide AST walk (cached, but the first caller pays it); the default 10s
# timeout is too tight on a loaded CI sandbox.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_wire_model_subclasses_never_re_forbid_extra() -> None:
    """No WireModel subclass anywhere in the repo may set extra="forbid".

    WireModel models parse remote_service_connector responses in shipped
    clients (the minds desktop app bundles them), so they are cross-version
    wire data exactly like EventEnvelope's persisted events: the base class
    deliberately ignores unknown fields so one additive server field never
    breaks an already-released client. A subclass re-tightening ``extra`` to
    ``"forbid"`` silently reintroduces that break for its endpoint, so it is
    banned outright. Subclass membership is computed transitively by class
    name across the whole repo (an over-approximation, which for a guard that
    should match nothing can only catch more).
    """
    base_names_by_class, forbid_locations_by_class = _collect_class_defs_for_model_config_checks()
    wire_model_class_names = _transitive_subclass_names(base_names_by_class, {"WireModel"})

    violations = [
        f"{class_name} at {location}"
        for class_name in sorted(wire_model_class_names)
        for location in forbid_locations_by_class.get(class_name, [])
    ]
    assert len(violations) == 0, (
        'WireModel subclasses must not set extra="forbid" (connector wire responses must tolerate '
        "additive fields so a server deploy never breaks already-shipped clients; see "
        "libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/wire.py):\n" + "\n".join(f"  - {v}" for v in violations)
    )


# Repo-wide AST walk (cached, but the first caller pays it); the default 10s
# timeout is too tight on a loaded CI sandbox.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_wire_types_files_contain_only_wire_models_and_wire_enums() -> None:
    """Every class in a wire_types.py must be (transitively) a WireModel or WireEnum.

    ``wire_types.py`` files hold connector response shapes by contract (see
    the module docstring in libs/mngr_imbue_cloud); a strict model or plain
    enum slipped in there would silently opt an endpoint out of the
    forward-compatibility guarantees. Bases are resolved transitively by
    class name across the repo, so intermediate bases defined elsewhere work.
    """
    base_names_by_class, _forbid_locations = _collect_class_defs_for_model_config_checks()
    tolerant_class_names = _transitive_subclass_names(base_names_by_class, {"WireModel", "WireEnum"})

    violations = []
    # The git-ls-files walk (cached, gitignore-pruned) instead of a raw rglob:
    # rglob descends into node_modules/.git/.venv and can blow the test timeout
    # on a slow sandbox.
    wire_types_paths = [
        path for path in _get_all_files_with_extension(_REPO_ROOT, ".py") if path.name == "wire_types.py"
    ]
    for wire_types_path in wire_types_paths:
        tree = ast.parse(wire_types_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name not in tolerant_class_names:
                violations.append(f"{node.name} at {wire_types_path.relative_to(_REPO_ROOT)}:{node.lineno}")
    assert len(violations) == 0, (
        "Every class in a wire_types.py must inherit WireModel or WireEnum (directly or transitively) "
        "so connector response shapes stay forward compatible:\n" + "\n".join(f"  - {v}" for v in violations)
    )


# --- Machine/workspace terminology (see specs/machine-workspace-naming/decisions.md) ---

# Non-test .py files under these globs are mngr-level: they speak host/agent and must
# not use the minds-level machine/workspace vocabulary or reference minds itself.
_MNGR_LEVEL_DIR_GLOB = "libs/mngr*"

# Test-infrastructure files are exempt: they may exercise higher-level scenarios and
# uv-workspace tooling, and their prose is not part of the shipped vocabulary.
_TERMINOLOGY_TEST_EXCLUSIONS: tuple[str, ...] = ("*_test.py", "test_*.py", "conftest.py", "testing.py")

# mngr_imbue_cloud carve-outs: the wire layer (and the primitives feeding it) mirrors
# the connector's own vocabulary, which says "workspace"; the bake/slice/admin-CLI
# operator tooling is minds-level infrastructure living in the plugin until it moves
# (https://github.com/imbue-ai/mngr-internal/issues/461).
_IMBUE_CLOUD_TERMINOLOGY_EXEMPT: tuple[str, ...] = (
    "wire.py",
    "wire_types.py",
    "primitives.py",
    "bake/*.py",
    "slices/*.py",
    "cli/*.py",
    "connector/*.py",
)

_PREVENT_WORKSPACE_VOCABULARY_IN_MNGR_LEVEL_CODE = RegexRatchetRule(
    rule_name="workspace vocabulary in mngr-level code",
    rule_description=(
        "mngr-level code (libs/mngr and the mngr plugins) speaks host/agent; 'workspace' is the "
        "minds-level term for the logical unit identified by its system-services agent id. Say "
        "host, agent, work dir, or project as appropriate (see "
        "specs/machine-workspace-naming/decisions.md). The uv sense must be spelled 'uv-workspace'."
    ),
    # The uv sense is allowed when spelled "uv-workspace" (the lookbehind).
    pattern_string=r"(?i)(?<!uv-)\bworkspaces?\b",
)

_PREVENT_MINDS_REFERENCES_IN_MNGR_LEVEL_CODE = RegexRatchetRule(
    rule_name="minds references in mngr-level code",
    rule_description=(
        "mngr-level code must not reference minds, default-workspace-template, or the "
        "/home/user/workspace container path -- those are higher-level concerns layered on top "
        "of mngr (see specs/machine-workspace-naming/decisions.md). Describe the behavior "
        "generically (e.g. 'a caller may...') instead of naming the higher-level product."
    ),
    pattern_string=r"(?i)\bminds\b|default[-_]workspace[-_]template|/home/user/workspace",
)


def _mngr_level_terminology_chunks(rule: RegexRatchetRule) -> list[RatchetMatchChunk]:
    chunks: list[RatchetMatchChunk] = []
    for level_dir in sorted(_REPO_ROOT.glob(_MNGR_LEVEL_DIR_GLOB)):
        if not level_dir.is_dir():
            continue
        exclusions = _TERMINOLOGY_TEST_EXCLUSIONS
        if level_dir.name == "mngr_imbue_cloud":
            exclusions = exclusions + _IMBUE_CLOUD_TERMINOLOGY_EXEMPT
        chunks.extend(check_ratchet_rule(rule, level_dir, exclusions))
    return chunks


def test_prevent_workspace_vocabulary_in_mngr_level_code() -> None:
    """Keep the minds-level 'workspace' vocabulary out of mngr-level code (count may only fall)."""
    chunks = _mngr_level_terminology_chunks(_PREVENT_WORKSPACE_VOCABULARY_IN_MNGR_LEVEL_CODE)
    assert len(chunks) <= snapshot(368), _PREVENT_WORKSPACE_VOCABULARY_IN_MNGR_LEVEL_CODE.format_failure(tuple(chunks))


def test_prevent_minds_references_in_mngr_level_code() -> None:
    """Keep minds / default-workspace-template references out of mngr-level code (count may only fall)."""
    chunks = _mngr_level_terminology_chunks(_PREVENT_MINDS_REFERENCES_IN_MNGR_LEVEL_CODE)
    assert len(chunks) <= snapshot(352), _PREVENT_MINDS_REFERENCES_IN_MNGR_LEVEL_CODE.format_failure(tuple(chunks))
