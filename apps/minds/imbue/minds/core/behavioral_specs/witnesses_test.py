"""Tests for the witnesses-marker scan and coordinate check."""

from pathlib import Path

from imbue.minds.core.behavioral_specs.witnesses import check_witness_markers
from imbue.minds.core.behavioral_specs.witnesses import find_witness_markers_in_paths
from imbue.minds.core.behavioral_specs.witnesses import find_witness_markers_in_source


def test_find_witness_markers_in_decorator_form(tmp_path: Path) -> None:
    source = (
        "import pytest\n"
        "\n"
        '@pytest.mark.witnesses("authentication.fresh-code")\n'
        "def test_signs_in() -> None: ...\n"
        "\n"
        '@pytest.mark.witnesses("authentication.prefetch", partial="does not assert the code stays unspent")\n'
        "def test_prefetch() -> None: ...\n"
    )

    scan = find_witness_markers_in_source(source, tmp_path / "test_auth.py")

    assert scan.problems == ()
    assert [(marker.coordinate, marker.line, marker.partial) for marker in scan.markers] == [
        ("authentication.fresh-code", 3, None),
        ("authentication.prefetch", 6, "does not assert the code stays unspent"),
    ]


def test_find_witness_markers_in_pytestmark_assignments(tmp_path: Path) -> None:
    source = (
        "import pytest\n"
        "\n"
        'pytestmark = pytest.mark.witnesses("single-use-codes")\n'
        "\n"
        "class TestSuite:\n"
        '    pytestmark = [pytest.mark.witnesses("authentication.fresh-code"), pytest.mark.acceptance]\n'
        "\n"
        "    def test_one(self) -> None: ...\n"
    )

    scan = find_witness_markers_in_source(source, tmp_path / "test_suite.py")

    assert scan.problems == ()
    assert [(marker.coordinate, marker.line) for marker in scan.markers] == [
        ("single-use-codes", 3),
        ("authentication.fresh-code", 6),
    ]


def test_find_witness_markers_flags_non_literal_coordinates(tmp_path: Path) -> None:
    source = (
        "import pytest\n"
        "\n"
        'COORDINATE = "authentication.fresh-code"\n'
        "\n"
        "@pytest.mark.witnesses(COORDINATE)\n"
        "def test_signs_in() -> None: ...\n"
    )

    scan = find_witness_markers_in_source(source, tmp_path / "test_auth.py")

    assert scan.markers == ()
    assert len(scan.problems) == 1
    assert scan.problems[0].line == 5
    assert "string literal" in scan.problems[0].message


def test_find_witness_markers_ignores_other_markers(tmp_path: Path) -> None:
    source = "import pytest\n\n@pytest.mark.acceptance\n@pytest.mark.timeout(30)\ndef test_unrelated() -> None: ...\n"

    scan = find_witness_markers_in_source(source, tmp_path / "test_other.py")

    assert scan.markers == ()
    assert scan.problems == ()


def test_find_witness_markers_in_paths_walks_directories(tmp_path: Path) -> None:
    tree = tmp_path / "tests"
    (tree / "sub").mkdir(parents=True)
    (tree / "test_a.py").write_text('import pytest\n\n@pytest.mark.witnesses("a.b")\ndef test_a() -> None: ...\n')
    (tree / "sub" / "test_b.py").write_text(
        'import pytest\n\n@pytest.mark.witnesses("c.d")\ndef test_b() -> None: ...\n'
    )
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "test_cached.py").write_text("not even python {\n")
    (tree / "not_a_test.txt").write_text("import pytest\n")

    scan = find_witness_markers_in_paths((tree,))

    assert scan.problems == ()
    assert {marker.coordinate for marker in scan.markers} == {"a.b", "c.d"}


def test_find_witness_markers_in_paths_reports_unparseable_files(tmp_path: Path) -> None:
    bad_file = tmp_path / "test_broken.py"
    bad_file.write_text("def test_broken(:\n")

    scan = find_witness_markers_in_paths((bad_file,))

    assert scan.markers == ()
    assert len(scan.problems) == 1
    assert "could not be parsed" in scan.problems[0].message


def test_check_witness_markers_reports_unknown_coordinates(tmp_path: Path) -> None:
    marker_source = 'import pytest\n\n@pytest.mark.witnesses("authentication.typoo")\ndef test_x() -> None: ...\n'
    test_file = tmp_path / "test_x.py"
    test_file.write_text(marker_source)
    scan = find_witness_markers_in_paths((test_file,))

    problems = check_witness_markers(scan, frozenset({"authentication.fresh-code"}))

    assert len(problems) == 1
    assert problems[0].line == 3
    assert "unknown coordinate 'authentication.typoo'" in problems[0].message


def test_check_witness_markers_accepts_known_coordinates_and_keeps_scan_problems(tmp_path: Path) -> None:
    test_file = tmp_path / "test_x.py"
    test_file.write_text(
        'import pytest\n\n@pytest.mark.witnesses("authentication.fresh-code")\ndef test_x() -> None: ...\n'
    )
    scan = find_witness_markers_in_paths((test_file,))

    problems = check_witness_markers(scan, frozenset({"authentication.fresh-code"}))

    assert problems == ()
