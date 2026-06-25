import textwrap

from scripts.changelog_projects import pyproject_projects
from scripts.sync_common_ratchets import EXCLUDED_RATCHET_PROJECTS
from scripts.sync_common_ratchets import REPO_ROOT
from scripts.sync_common_ratchets import RatchetTemplate
from scripts.sync_common_ratchets import _discover_check_functions
from scripts.sync_common_ratchets import _extract_tests
from scripts.sync_common_ratchets import _find_test_ratchet_files
from scripts.sync_common_ratchets import _insert_test
from test_meta_ratchets import _EXCLUDED_PROJECTS


def test_find_test_ratchet_files_covers_every_pyproject_project() -> None:
    """The discovered test_ratchets.py files must correspond exactly to the (non-excluded)
    pyproject projects, cross-checked against the independent ``pyproject_projects`` discovery.

    This catches a project being added without a ratchet file, or one being dropped, rather than
    merely asserting a hand-guessed lower bound on the file count.
    """
    files = _find_test_ratchet_files()
    discovered_projects = {f.relative_to(REPO_ROOT).parts[1] for f in files}
    expected_projects = set(pyproject_projects(REPO_ROOT)) - EXCLUDED_RATCHET_PROJECTS
    assert discovered_projects == expected_projects
    for f in files:
        assert f.name == "test_ratchets.py"
        assert f.exists()


def test_extract_tests_finds_all_functions() -> None:
    source = textwrap.dedent("""\
        from pathlib import Path

        import pytest
        from inline_snapshot import snapshot

        _DIR = Path(__file__).parent

        pytestmark = pytest.mark.xdist_group(name="ratchets")

        # --- Code safety ---


        def test_prevent_todos() -> None:
            rc.check_todos(_DIR, snapshot(0))


        @pytest.mark.timeout(10)
        def test_prevent_args() -> None:
            rc.check_args(_DIR, snapshot(3))


        # --- Exception handling ---


        def test_prevent_bare_except() -> None:
            rc.check_bare_except(_DIR, snapshot(0))
    """)
    tests = _extract_tests(source)
    names = {t.name for t in tests}
    assert names == {"test_prevent_todos", "test_prevent_args", "test_prevent_bare_except"}

    by_name = {t.name: t for t in tests}
    assert by_name["test_prevent_todos"].section == "Code safety"
    assert by_name["test_prevent_args"].section == "Code safety"
    assert by_name["test_prevent_bare_except"].section == "Exception handling"

    assert "@pytest.mark.timeout(10)" in by_name["test_prevent_args"].source


def test_insert_test_into_existing_section() -> None:
    source = textwrap.dedent("""\
        # --- Code safety ---


        def test_prevent_todos() -> None:
            rc.check_todos(_DIR, snapshot(0))


        # --- Exception handling ---


        def test_prevent_bare_except() -> None:
            rc.check_bare_except(_DIR, snapshot(0))
    """)
    template = RatchetTemplate(
        name="test_prevent_new",
        source="def test_prevent_new() -> None:\n    rc.check_new(_DIR, snapshot(0))",
        section="Code safety",
    )
    lines = source.splitlines()
    result_lines = _insert_test(lines, template)
    result = "\n".join(result_lines)

    assert "test_prevent_new" in result
    code_safety_pos = result.index("Code safety")
    new_test_pos = result.index("test_prevent_new")
    exception_pos = result.index("Exception handling")
    assert code_safety_pos < new_test_pos < exception_pos


def test_insert_test_creates_new_section() -> None:
    source = textwrap.dedent("""\
        # --- Code safety ---


        def test_prevent_todos() -> None:
            rc.check_todos(_DIR, snapshot(0))


        # --- Exception handling ---


        def test_prevent_bare_except() -> None:
            rc.check_bare_except(_DIR, snapshot(0))
    """)
    template = RatchetTemplate(
        name="test_prevent_model_copy",
        source="def test_prevent_model_copy() -> None:\n    rc.check_model_copy(_DIR, snapshot(0))",
        section="Pydantic / models",
    )
    lines = source.splitlines()
    result_lines = _insert_test(lines, template)
    result = "\n".join(result_lines)

    assert "# --- Pydantic / models ---" in result
    assert "test_prevent_model_copy" in result
    exception_pos = result.index("Exception handling")
    pydantic_pos = result.index("Pydantic / models")
    assert exception_pos < pydantic_pos


def test_excluded_projects_in_sync() -> None:
    """The sync script and meta ratchets must agree on which projects are excluded."""
    assert EXCLUDED_RATCHET_PROJECTS == _EXCLUDED_PROJECTS


def test_discover_check_functions_finds_all() -> None:
    """Verify the script discovers all check functions from standard_ratchet_checks.py."""
    templates = _discover_check_functions()
    names = {t.name for t in templates}
    # Every discovered check must yield a uniquely-named test_prevent_* template under a real
    # section (_discover_check_functions raises rather than emitting "Unknown").
    assert templates
    assert len(names) == len(templates)
    assert "test_prevent_todos" in names
    assert "test_prevent_bare_except" in names
    assert "test_prevent_code_in_init_files" in names
    for t in templates:
        assert t.name.startswith("test_prevent_")
        assert t.section != "Unknown"


def test_script_reports_all_in_sync() -> None:
    """Verify the script's discovery and parsing works on the real repo: every discovered
    test_ratchets.py must yield at least one extracted test."""
    files = _find_test_ratchet_files()
    assert files
    for f in files:
        tests = _extract_tests(f.read_text())
        assert len(tests) > 0, f"{f}: no tests extracted"
