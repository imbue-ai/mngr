import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent

from scripts.junit_test_summary import AttemptsRecord
from scripts.junit_test_summary import FailureDetail
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
    per_test, failures = _parse_junit(junit)
    assert per_test["pkg/test_x.py::test_a"].attempts == 2
    assert per_test["pkg/test_x.py::test_a"].passed == 2
    assert per_test["pkg/test_x.py::test_a"].final_status is RunStatus.PASSED
    assert per_test["pkg/test_x.py::test_b"].attempts == 2
    assert per_test["pkg/test_x.py::test_b"].failed == 1
    assert per_test["pkg/test_x.py::test_b"].passed == 1
    assert per_test["pkg/test_x.py::test_b"].final_status is RunStatus.FLAKY_RECOVERED
    assert [(f.name, f.kind, f.message, f.attempt) for f in failures] == [
        ("pkg/test_x.py::test_b", "failure", "boom", 1),
    ]


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
        failures=[],
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

    md = _render_markdown(per_test=per_test, failures=[], flaky_ids=set(), heading="H", max_chars=1000)
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
    full = _render_markdown(per_test=per_test, failures=[], flaky_ids=set(), heading="H", max_chars=10_000)
    # Pick a max_chars that fits the full output exactly but is below the
    # old (body + footer-headroom) budget, so the previous implementation
    # would have truncated.
    tight = _render_markdown(per_test=per_test, failures=[], flaky_ids=set(), heading="H", max_chars=len(full))
    assert tight == full
    assert "additional test row(s) omitted" not in tight
    # Every test row must still be present.
    for i in range(5):
        assert f"pkg/test.py::test_{i}" in tight


def test_render_markdown_empty_run() -> None:
    md = _render_markdown(per_test={}, failures=[], flaky_ids=set(), heading="H", max_chars=10_000)
    assert "No tests recorded" in md


def test_render_markdown_includes_failure_details_section() -> None:
    """Failed/errored attempts must surface their captured traceback inline.

    The check-run summary on the PR-checks page is the only place a reader
    sees the failure body without clicking through to the workflow run, so
    a failure section with the actual error text is part of the contract.
    """
    a = AttemptsRecord(name="pkg/test_x.py::test_a")
    a.record(outcome=RunStatus.FAILED)
    a.record(outcome=RunStatus.PASSED)
    failures = [
        FailureDetail(
            name="pkg/test_x.py::test_a",
            kind="failure",
            message="AssertionError: expected 1 got 2",
            body="Traceback (most recent call last):\n  File 'x', line 1\n    assert 1 == 2\nAssertionError",
            attempt=1,
        ),
    ]

    md = _render_markdown(
        per_test={a.name: a},
        failures=failures,
        flaky_ids=set(),
        heading="H",
        max_chars=10_000,
    )
    assert "## Failures" in md
    assert "<details><summary>" in md
    assert "pkg/test_x.py::test_a" in md
    # First line of the failure message appears in the collapsed summary.
    assert "AssertionError: expected 1 got 2" in md
    # The captured body text is rendered inside the details block.
    assert "assert 1 == 2" in md
    # Failures section comes before the table so readers see errors first.
    assert md.index("## Failures") < md.index("| Test |")


def test_render_markdown_caps_long_failure_body() -> None:
    """A single huge traceback must not crowd out the rest of the summary."""
    a = AttemptsRecord(name="pkg/test_x.py::test_a")
    a.record(outcome=RunStatus.FAILED)
    huge = "X" * 50_000
    failures = [FailureDetail(name=a.name, kind="failure", message="boom", body=huge, attempt=1)]

    md = _render_markdown(
        per_test={a.name: a},
        failures=failures,
        flaky_ids=set(),
        heading="H",
        max_chars=20_000,
    )
    assert "[truncated to" in md
    # The capped body is much smaller than the original 50k chars.
    assert md.count("X") < 5_000


def test_render_markdown_labels_attempt_index_for_retried_tests() -> None:
    """Failures of a retried test must be labelled `attempt N/M`.

    A reader looking at a flaky-recovered test should be able to tell which
    attempt the failure belonged to (likely a flake) without expanding the
    block. Tests that ran once must NOT get the label, since "attempt 1/1"
    is noise on a hard-failed test.
    """
    # Flaky-recovered: failed on attempt 1, passed on attempt 2.
    flaky = AttemptsRecord(name="pkg/test_x.py::test_flaky")
    flaky.record(outcome=RunStatus.FAILED)
    flaky.record(outcome=RunStatus.PASSED)
    # Hard failure: one attempt, failed.
    hard = AttemptsRecord(name="pkg/test_y.py::test_hard")
    hard.record(outcome=RunStatus.FAILED)

    failures = [
        FailureDetail(
            name=flaky.name,
            kind="failure",
            message="TimeoutError: connection refused",
            body="trace-1",
            attempt=1,
        ),
        FailureDetail(
            name=hard.name,
            kind="failure",
            message="AssertionError: nope",
            body="trace-2",
            attempt=1,
        ),
    ]
    md = _render_markdown(
        per_test={flaky.name: flaky, hard.name: hard},
        failures=failures,
        flaky_ids=set(),
        heading="H",
        max_chars=10_000,
    )
    # Retried test: attempt label appears in the <summary>.
    assert "<code>pkg/test_x.py::test_flaky</code> (attempt 1/2) &mdash; failure" in md
    # Single-attempt test: no attempt label.
    assert "<code>pkg/test_y.py::test_hard</code> &mdash; failure" in md
    assert "pkg/test_y.py::test_hard</code> (attempt" not in md


def test_render_markdown_escapes_html_in_failure_summary() -> None:
    """Failure message/name in <summary> must be HTML-escaped so it renders literally."""
    a = AttemptsRecord(name="pkg/<weird>::test_a")
    a.record(outcome=RunStatus.FAILED)
    failures = [
        FailureDetail(
            name=a.name,
            kind="failure",
            message="ValueError: <not a tag> & co",
            body="trace",
            attempt=1,
        ),
    ]
    md = _render_markdown(
        per_test={a.name: a},
        failures=failures,
        flaky_ids=set(),
        heading="H",
        max_chars=10_000,
    )
    # Angle brackets in the message line and the name must be escaped so
    # they render as text instead of being parsed as HTML by GitHub.
    assert "&lt;not a tag&gt;" in md
    assert "&amp; co" in md
    assert "pkg/&lt;weird&gt;::test_a" in md
