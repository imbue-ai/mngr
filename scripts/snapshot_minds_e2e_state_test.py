"""Tests for the CI wiring around ``scripts/snapshot_minds_e2e_state.py``.

``offload-modal-minds-snapshot.toml`` scopes offload's test discovery to an
explicit list of paths (``[framework].paths``) because collecting the whole
monorepo just to find the ``minds_snapshot_resume`` tests cost ~90s on every
CI run. The correctness risk of scoping is that a snapshot-resume test added
in a NEW file would silently never be discovered (and thus never run) in CI.
The test below pins the config's path list to exactly the set of test files
that apply the mark, so that drift fails loudly here instead.
"""

import re
import shlex
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

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
    group. (A nested-pytest replay of discovery is not possible here: it
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
