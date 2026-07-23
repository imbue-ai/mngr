"""Tests for the CI wiring around ``scripts/snapshot_minds_e2e_state.py``.

``offload-modal-minds-snapshot.toml`` scopes offload's test discovery to an
explicit list of paths (``[framework].paths``) because collecting the whole
monorepo just to find the ``minds_snapshot_resume`` tests cost ~90s on every
CI run. The correctness risk of scoping is that a snapshot-resume test added
in a NEW file would silently never be discovered (and thus never run) in CI.
The test below pins the config's path list to exactly the set of test files
that apply the mark, so that drift fails loudly here instead.

The remaining tests pin the dependency-manifest staging used to make the
snapshot image's third-party-install layers cacheable across CI runs: the
python manifests tree must cover every uv workspace member's pyproject.toml
(a missing member manifest breaks the manifests-only
``uv sync --no-install-workspace`` layer), and the pnpm tree must contain
exactly what ``pnpm install --frozen-lockfile`` reads.
"""

import re
import shlex
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

from scripts.snapshot_minds_e2e_state import _PNPM_MANIFEST_RELATIVE_PATHS
from scripts.snapshot_minds_e2e_state import _python_manifest_relative_paths
from scripts.snapshot_minds_e2e_state import _stage_dep_manifest_trees

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_OFFLOAD_CONFIG_PATH: Final[Path] = _REPO_ROOT / "offload-modal-minds-snapshot.toml"

# Matches the ways the mark is applied to a test: the per-test decorator
# form (``@pytest.mark.<mark-name>``) and module-level ``pytestmark`` lists.
# The mark *registration* in apps/minds/conftest.py does not match (no
# ``mark.`` prefix) -- and conftest.py is not a collected test file anyway.
# Note this file's own comments must never spell out the literal
# ``mark.<mark-name>`` string, or this guard would match itself.
_MARK_APPLICATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"mark\.minds_snapshot_resume\b")

# Directories that never contain repo test files; pruning them keeps the scan
# cheap and avoids matching vendored fixtures.
_PRUNED_DIR_NAMES: Final[frozenset[str]] = frozenset({".venv", "node_modules", ".git", ".external_worktrees"})


def _is_test_file(path: Path) -> bool:
    """Return whether ``path`` matches pytest's default file patterns."""
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def _find_snapshot_resume_marked_test_files() -> set[str]:
    """Return repo-relative paths of collected test files applying the mark.

    Mirrors the root pyproject's ``testpaths`` (repo-root ``test_*.py`` plus
    ``apps/``, ``libs/``, ``scripts/``), filtered to pytest's default
    ``test_*.py`` / ``*_test.py`` file patterns.
    """
    marked_files: set[str] = set()
    candidates: list[Path] = [path for path in _REPO_ROOT.glob("test_*.py") if path.is_file()]
    for search_root_name in ("apps", "libs", "scripts"):
        search_root = _REPO_ROOT / search_root_name
        candidates.extend(
            path
            for path in search_root.rglob("*.py")
            if _is_test_file(path) and not _PRUNED_DIR_NAMES.intersection(path.parts)
        )
    for candidate in candidates:
        if _MARK_APPLICATION_PATTERN.search(candidate.read_text()):
            marked_files.add(candidate.relative_to(_REPO_ROOT).as_posix())
    return marked_files


def _read_offload_config() -> dict[str, Any]:
    return tomllib.loads(_OFFLOAD_CONFIG_PATH.read_text())


def _read_offload_discovery_paths() -> set[str]:
    """Return the ``[framework].paths`` list from the minds-snapshot offload config."""
    config = _read_offload_config()
    framework_config = config["framework"]
    assert framework_config["type"] == "pytest"
    return set(framework_config["paths"])


def test_offload_discovery_paths_match_marked_test_files() -> None:
    discovery_paths = _read_offload_discovery_paths()
    marked_files = _find_snapshot_resume_marked_test_files()
    assert discovery_paths == marked_files, (
        "offload-modal-minds-snapshot.toml's [framework].paths must list exactly the test files "
        "that apply the minds_snapshot_resume mark (discovery is scoped to these paths to avoid a "
        "~90s full-monorepo collection on every CI run).\n"
        f"In config but no marked tests found: {sorted(discovery_paths - marked_files)}\n"
        f"Marked tests but missing from config (these would silently never run in CI): "
        f"{sorted(marked_files - discovery_paths)}\n"
        "Fix: update the paths list in offload-modal-minds-snapshot.toml."
    )


def test_offload_discovery_filters_pin_repo_root_pytest_config() -> None:
    """Assert every offload group's discovery filters pin the repo-root pytest config.

    offload passes discovered test ids verbatim as argv to pytest at
    execution time, where the cwd is the repo root (/code/mngr in the
    sandbox). Scoped ``paths`` under apps/minds would make pytest pick
    apps/minds as rootdir (apps/minds/pyproject.toml has a pytest section)
    and emit ids like ``test_snapshot_resume.py::...`` that do NOT resolve
    from the repo root, so every test would error at execution. The
    ``-c pyproject.toml`` discovery arg in the group filters keeps the ids
    repo-root-relative; this test fails if that pinning is dropped from any
    group. Remove both the flag and this test once OFFLOAD-9 gives offload a
    first-class discovery-args/rootdir knob. (A nested-pytest replay of
    discovery is not possible here: it
    would run with PYTEST_CURRENT_TEST set and trip the mngr config loader's
    is_allowed_in_pytest guard on the repo's .mngr/settings.toml.)
    """
    config = _read_offload_config()
    groups = config["groups"]
    assert groups, "offload-modal-minds-snapshot.toml has no [groups]"
    for group_name, group_config in groups.items():
        filter_args = shlex.split(group_config["filters"])
        config_flag_index = next((index for index, arg in enumerate(filter_args) if arg == "-c"), None)
        assert config_flag_index is not None, (
            f"Group {group_name!r} filters must include `-c pyproject.toml` so discovery emits "
            "repo-root-relative test ids -- see the [framework] comment in "
            "offload-modal-minds-snapshot.toml."
        )
        assert filter_args[config_flag_index + 1] == "pyproject.toml", (
            f"Group {group_name!r} filters must pass the REPO-ROOT pyproject.toml to `-c` "
            "(relative to the repo root, which is the discovery cwd)."
        )


def test_python_manifest_paths_cover_all_workspace_members() -> None:
    """The staged python manifests must include every uv workspace member's pyproject.toml.

    The manifests-only image layer runs ``uv sync --all-packages --no-install-workspace``,
    which constructs the workspace from the member manifests. A member added
    under a directory pattern not covered by the script's globs (e.g. a new
    ``tools/*`` entry in ``[tool.uv.workspace].members``) would be missing
    from that layer and break the image build -- so the script's globs must
    cover every member pattern the root pyproject declares.
    """
    root_pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    member_patterns: list[str] = root_pyproject["tool"]["uv"]["workspace"]["members"]
    expected_member_manifests = {
        path.relative_to(_REPO_ROOT).as_posix()
        for pattern in member_patterns
        for path in _REPO_ROOT.glob(f"{pattern}/pyproject.toml")
    }
    assert expected_member_manifests, "no uv workspace members found -- is the test reading the right repo?"
    staged_paths = set(_python_manifest_relative_paths(_REPO_ROOT))
    assert "pyproject.toml" in staged_paths
    assert "uv.lock" in staged_paths
    missing_manifests = expected_member_manifests - staged_paths
    assert not missing_manifests, (
        "These uv workspace member manifests are NOT staged into the snapshot image's python "
        f"manifests layer: {sorted(missing_manifests)}. Update _PY_WORKSPACE_MEMBER_MANIFEST_GLOBS "
        "in scripts/snapshot_minds_e2e_state.py to cover them."
    )


def test_stage_dep_manifest_trees_copy_only_manifest_files(tmp_path: Path) -> None:
    """The staged trees must contain ONLY dependency manifests (else the cache key wobbles).

    The whole point of the manifests trees is that their Modal layer hash is
    stable across source-only commits; a stray source file copied into them
    would bust the cached install layers on every commit.
    """
    python_tree, pnpm_tree = _stage_dep_manifest_trees(_REPO_ROOT, tmp_path)
    python_files = {path.relative_to(python_tree).as_posix() for path in python_tree.rglob("*") if path.is_file()}
    assert python_files == set(_python_manifest_relative_paths(_REPO_ROOT))
    assert all(Path(path).name in ("pyproject.toml", "uv.lock") for path in python_files)
    pnpm_files = {path.relative_to(pnpm_tree).as_posix() for path in pnpm_tree.rglob("*") if path.is_file()}
    assert pnpm_files == set(_PNPM_MANIFEST_RELATIVE_PATHS)
