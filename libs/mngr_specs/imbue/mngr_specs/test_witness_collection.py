"""Integration test for the witness harvest: a real inner ``pytest --collect-only``.

The synthetic test tree is built under ``tmp_path`` (outside the repo), so no
repo conftest loads and the inner run collects only the tiny file written here.
The inner run warns about the unregistered ``witnesses`` marker (there is no
conftest to register it out there); that warning must not fail the harvest.
"""

from pathlib import Path

from imbue.mngr_specs.witnesses import harvest_witness_links

_SYNTHETIC_TEST_FILE = """
import pytest


@pytest.mark.witnesses("some.coordinate")
def test_plain() -> None:
    pass


@pytest.mark.witnesses("other.coordinate", partial="does not assert the code remains unspent")
def test_partial() -> None:
    pass


@pytest.mark.witnesses("first.coordinate")
@pytest.mark.witnesses("second.coordinate")
def test_two_markers() -> None:
    pass


@pytest.mark.witnesses()
def test_no_args() -> None:
    pass
"""


def _write_synthetic_tests(tests_dir: Path) -> Path:
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_sample.py").write_text(_SYNTHETIC_TEST_FILE, encoding="utf-8")
    return tests_dir


def test_harvest_witness_links_collects_every_marker_from_a_real_pytest_run(tmp_path: Path) -> None:
    tests_dir = _write_synthetic_tests(tmp_path / "synthetic_tests")

    links = harvest_witness_links([tests_dir])

    harvested = [(link.test.split("::")[-1], link.coordinate, link.partial) for link in links]
    assert harvested == [
        ("test_plain", "some.coordinate", None),
        ("test_partial", "other.coordinate", "does not assert the code remains unspent"),
        ("test_two_markers", "second.coordinate", None),
        ("test_two_markers", "first.coordinate", None),
        ("test_no_args", None, None),
    ]
    # Every link's node id is file-qualified (``<path>::<test>``) and names the synthetic file.
    assert all("test_sample.py::" in link.test for link in links)


def test_harvest_witness_links_returns_nothing_for_a_tree_with_no_markers(tmp_path: Path) -> None:
    tests_dir = tmp_path / "empty_tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_bare.py").write_text("def test_nothing() -> None:\n    pass\n", encoding="utf-8")

    assert harvest_witness_links([tests_dir]) == ()
