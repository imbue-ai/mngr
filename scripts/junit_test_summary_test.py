import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent

from scripts.junit_test_summary import AttemptsRecord
from scripts.junit_test_summary import RunStatus
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
    assert per_test["pkg/test_x.py::test_a"].final_status is RunStatus.PASSED
    assert per_test["pkg/test_x.py::test_b"].attempts == 2
    assert per_test["pkg/test_x.py::test_b"].failed == 1
    assert per_test["pkg/test_x.py::test_b"].passed == 1
    assert per_test["pkg/test_x.py::test_b"].final_status is RunStatus.FLAKY_RECOVERED


def test_testcase_outcome_prefers_failure_over_skip() -> None:
    tc = ET.fromstring('<testcase name="x" time="0.1"><failure message="m"/><skipped/></testcase>')
    assert _testcase_outcome(tc) is RunStatus.FAILED


def test_testcase_outcome_skipped() -> None:
    tc = ET.fromstring('<testcase name="x" time="0.1"><skipped/></testcase>')
    assert _testcase_outcome(tc) is RunStatus.SKIPPED


def test_testcase_outcome_passed() -> None:
    tc = ET.fromstring('<testcase name="x" time="0.1"/>')
    assert _testcase_outcome(tc) is RunStatus.PASSED


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


def test_render_markdown_shows_every_test_with_runs_and_flaky() -> None:
    # test_a: flaky-recovered (passed, failed, passed), marked @flaky
    a = AttemptsRecord(name="pkg/test_x.py::test_a")
    a.record(outcome=RunStatus.PASSED)
    a.record(outcome=RunStatus.FAILED)
    a.record(outcome=RunStatus.PASSED)
    # test_b: passed once, not flaky-marked -- MUST still appear in the table
    b = AttemptsRecord(name="pkg/test_x.py::test_b")
    b.record(outcome=RunStatus.PASSED)
    # test_c: failed every time
    c = AttemptsRecord(name="pkg/test_x.py::test_c")
    c.record(outcome=RunStatus.FAILED)
    c.record(outcome=RunStatus.FAILED)
    per_test = {a.name: a, b.name: b, c.name: c}

    md = _render_markdown(
        per_test=per_test,
        flaky_ids={"pkg/test_x.py::test_a"},
        heading="H",
        max_chars=10_000,
    )
    assert "## H" in md
    assert "Unique tests: **3**" in md
    assert "Total runs (attempts across retries): **6**" in md
    assert "Tests marked `@pytest.mark.flaky`: **1**" in md

    _, _, table = md.partition("| Test |")
    # Every test -- including the single-run non-flaky one -- must be in the table.
    assert "pkg/test_x.py::test_a" in table
    assert "pkg/test_x.py::test_b" in table
    assert "pkg/test_x.py::test_c" in table
    # The single-run test is shown with Runs=1 and flaky=no.
    assert "| `pkg/test_x.py::test_b` | 1 | passed | no |" in table
    # Flaky-marked test is shown with flaky=yes, and the Final cell expands
    # "flaky-recovered" into "flaked N, passed M" so the reader can see the
    # fail/pass breakdown across attempts.
    assert "| `pkg/test_x.py::test_a` | 3 | flaked 1, passed 2 | yes |" in table
    # Problem rows sort before plain passes so they survive truncation.
    failed_idx = table.index("pkg/test_x.py::test_c")
    passed_idx = table.index("pkg/test_x.py::test_b")
    assert failed_idx < passed_idx


def test_render_markdown_truncates_over_max_chars() -> None:
    per_test: dict[str, AttemptsRecord] = {}
    for i in range(100):
        name = f"pkg/test_big.py::test_{i:03d}"
        r = AttemptsRecord(name=name)
        r.record(outcome=RunStatus.PASSED)
        per_test[name] = r

    md = _render_markdown(per_test=per_test, flaky_ids=set(), heading="H", max_chars=1000)
    assert len(md) <= 1000
    assert "additional test row(s) omitted" in md
    # Still surfaces the stats section even when the table is truncated.
    assert "Unique tests: **100**" in md


def test_render_markdown_keeps_all_rows_when_body_fits_without_footer() -> None:
    """When the full table fits under max_chars we must keep every row and skip the footer.

    Regression: previously the renderer always reserved footer headroom from the
    budget, so a table whose full body was just under max_chars was still
    truncated and got an "N additional test row(s) omitted" footer.
    """
    per_test: dict[str, AttemptsRecord] = {}
    for i in range(5):
        name = f"pkg/test.py::test_{i}"
        r = AttemptsRecord(name=name)
        r.record(outcome=RunStatus.PASSED)
        per_test[name] = r

    # Render without a cap so we know the unconstrained size.
    full = _render_markdown(per_test=per_test, flaky_ids=set(), heading="H", max_chars=10_000)
    # Pick a max_chars that fits the full output exactly but is below the
    # old (body + 160-char footer headroom) budget, so the previous
    # implementation would have truncated.
    tight = _render_markdown(per_test=per_test, flaky_ids=set(), heading="H", max_chars=len(full))
    assert tight == full
    assert "additional test row(s) omitted" not in tight
    # Every test row must still be present.
    for i in range(5):
        assert f"pkg/test.py::test_{i}" in tight


def test_render_markdown_empty_run() -> None:
    md = _render_markdown(per_test={}, flaky_ids=set(), heading="H", max_chars=10_000)
    assert "No tests recorded" in md
