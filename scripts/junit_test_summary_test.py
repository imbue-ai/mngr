from pathlib import Path
from textwrap import dedent

from scripts.junit_test_summary import AttemptsRecord
from scripts.junit_test_summary import _load_flaky_manifest
from scripts.junit_test_summary import _parse_junit
from scripts.junit_test_summary import _render_markdown
from scripts.junit_test_summary import _testcase_outcome


def _write_junit(path: Path, testsuites_xml: str) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuites name="offload" tests="0" failures="0" errors="0" time="0">' + testsuites_xml + "</testsuites>"
    )


def test_parse_junit_counts_attempts(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    _write_junit(
        junit,
        dedent(
            """\
            <testsuite name="a"><testcase name="pkg/test_x.py::test_a" time="0.5"/></testsuite>
            <testsuite name="b"><testcase name="pkg/test_x.py::test_a" time="0.6"/></testsuite>
            <testsuite name="c"><testcase name="pkg/test_x.py::test_b" time="1.0"><failure message="boom"/></testcase></testsuite>
            <testsuite name="d"><testcase name="pkg/test_x.py::test_b" time="0.9"/></testsuite>
            """
        ),
    )
    per_test = _parse_junit(junit)
    assert per_test["pkg/test_x.py::test_a"].attempts == 2
    assert per_test["pkg/test_x.py::test_a"].passed == 2
    assert per_test["pkg/test_x.py::test_a"].final_status == "passed"
    assert per_test["pkg/test_x.py::test_b"].attempts == 2
    assert per_test["pkg/test_x.py::test_b"].failed == 1
    assert per_test["pkg/test_x.py::test_b"].passed == 1
    assert per_test["pkg/test_x.py::test_b"].final_status == "flaky-recovered"


def test_testcase_outcome_prefers_failure_over_skip(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    tc = ET.fromstring('<testcase name="x" time="0.1"><failure message="m"/><skipped/></testcase>')
    assert _testcase_outcome(tc) == "failed"


def test_testcase_outcome_skipped(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    tc = ET.fromstring('<testcase name="x" time="0.1"><skipped/></testcase>')
    assert _testcase_outcome(tc) == "skipped"


def test_testcase_outcome_passed(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    tc = ET.fromstring('<testcase name="x" time="0.1"/>')
    assert _testcase_outcome(tc) == "passed"


def test_load_flaky_manifest_unions_files(tmp_path: Path) -> None:
    d1 = tmp_path / "sb-1" / ".test_output"
    d1.mkdir(parents=True)
    (d1 / "flaky_tests_a.txt").write_text("pkg/test_x.py::test_a\npkg/test_x.py::test_b\n")
    d2 = tmp_path / "sb-2" / ".test_output"
    d2.mkdir(parents=True)
    (d2 / "flaky_tests_b.txt").write_text("pkg/test_x.py::test_b\npkg/test_y.py::test_c\n")
    ids = _load_flaky_manifest(str(tmp_path / "**" / "flaky_tests_*.txt"))
    assert ids == {
        "pkg/test_x.py::test_a",
        "pkg/test_x.py::test_b",
        "pkg/test_y.py::test_c",
    }


def test_render_markdown_marks_flaky_tests() -> None:
    a = AttemptsRecord(name="pkg/test_x.py::test_a")
    a.record(outcome="passed")
    a.record(outcome="failed")
    a.record(outcome="passed")
    b = AttemptsRecord(name="pkg/test_x.py::test_b")
    b.record(outcome="passed")
    per_test = {a.name: a, b.name: b}
    md = _render_markdown(
        per_test=per_test,
        flaky_ids={"pkg/test_x.py::test_a"},
        heading="H",
    )
    assert "## H" in md
    assert "Unique tests: **2**" in md
    assert "Total attempts: **4**" in md
    assert "Tests marked `@pytest.mark.flaky`: **1**" in md
    # The flaky-recovered test should appear in the table and be marked yes.
    assert "pkg/test_x.py::test_a" in md
    assert "flaky-recovered" in md
    # The single-attempt passing, non-flaky test should NOT appear in the details table.
    _, _, table = md.partition("| Test |")
    assert "pkg/test_x.py::test_b" not in table


def test_render_markdown_empty_interesting() -> None:
    t = AttemptsRecord(name="pkg/test_x.py::test_a")
    t.record(outcome="passed")
    md = _render_markdown(per_test={t.name: t}, flaky_ids=set(), heading="H")
    assert "No retries, failures, or flaky-marked tests" in md
