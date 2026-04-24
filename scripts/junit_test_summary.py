#!/usr/bin/env python3
"""Generate a markdown summary of an offload junit.xml with per-test retry counts.

Each unique testcase in junit.xml may appear multiple times (one `<testcase>`
element per attempt) because offload runs tests multiple times -- either as
explicit retries of flaky-marked tests or because of its retry_count settings.
This script aggregates those attempts per test and emits a GitHub-flavored
markdown summary showing, for each test:

  - total number of attempts
  - retry count (attempts - 1)
  - pass/fail/skip breakdown
  - whether the test is marked `@pytest.mark.flaky`

Flaky-mark detection reads per-sandbox manifest files written by the
`_write_flaky_manifest` conftest hook. Offload downloads `.test_output/**`
from each sandbox, so the union of all `flaky_tests_*.txt` files under
`test-results/` is the authoritative set of flaky-marked test IDs for the run.
"""

import argparse
import glob
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from enum import StrEnum
from pathlib import Path
from typing import Final


class TestStatus(StrEnum):
    """The status of a single test attempt or the final aggregated status of a test.

    String values are the tokens that appear in the rendered markdown output, so
    they double as the public contract of this script.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    FLAKY_RECOVERED = "flaky-recovered"
    UNKNOWN = "unknown"


_STATUS_ORDER: Final[dict[TestStatus, int]] = {
    TestStatus.FAILED: 0,
    TestStatus.ERROR: 1,
    TestStatus.FLAKY_RECOVERED: 2,
    TestStatus.PASSED: 3,
    TestStatus.SKIPPED: 4,
    TestStatus.UNKNOWN: 5,
}


class AttemptsRecord:
    """Aggregated outcomes for a single test across all of its attempts."""

    __slots__ = ("name", "attempts", "passed", "failed", "errors", "skipped")

    def __init__(self, name: str) -> None:
        self.name: str = name
        self.attempts: int = 0
        self.passed: int = 0
        self.failed: int = 0
        self.errors: int = 0
        self.skipped: int = 0

    def record(self, outcome: TestStatus | str) -> None:
        status = TestStatus(outcome)
        self.attempts += 1
        if status is TestStatus.PASSED:
            self.passed += 1
        elif status is TestStatus.FAILED:
            self.failed += 1
        elif status is TestStatus.ERROR:
            self.errors += 1
        elif status is TestStatus.SKIPPED:
            self.skipped += 1

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)

    @property
    def final_status(self) -> TestStatus:
        # A test counts as passed overall if any attempt passed. If it also had
        # failures, highlight that it was flaky-recovered. A test with only
        # skips is reported as skipped (never counted as passed).
        if self.passed > 0:
            if self.failed > 0 or self.errors > 0:
                return TestStatus.FLAKY_RECOVERED
            return TestStatus.PASSED
        if self.failed > 0:
            return TestStatus.FAILED
        if self.errors > 0:
            return TestStatus.ERROR
        if self.skipped > 0:
            return TestStatus.SKIPPED
        return TestStatus.UNKNOWN


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", required=True, type=Path, help="Path to junit.xml.")
    parser.add_argument(
        "--flaky-manifest-glob",
        default="test-results/**/flaky_tests_*.txt",
        help=(
            "Glob (recursive) matching per-sandbox flaky manifest files written by "
            "the _write_flaky_manifest conftest hook."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write markdown to this file (default: stdout).",
    )
    parser.add_argument(
        "--heading",
        default="Test retry + flaky summary",
        help="Heading to use at the top of the markdown output.",
    )
    args = parser.parse_args()

    if not args.junit.is_file():
        print(f"junit file not found: {args.junit}", file=sys.stderr)
        return 1

    flaky_ids = _load_flaky_manifest(args.flaky_manifest_glob)
    per_test = _parse_junit(args.junit)
    markdown = _render_markdown(per_test=per_test, flaky_ids=flaky_ids, heading=args.heading)

    if args.output is not None:
        args.output.write_text(markdown)
    else:
        sys.stdout.write(markdown)
    return 0


def _load_flaky_manifest(glob_pattern: str) -> set[str]:
    # include_hidden=True so we match files under dotted directories like
    # `.test_output/` that offload downloads back from each sandbox.
    ids: set[str] = set()
    for match in glob.glob(glob_pattern, recursive=True, include_hidden=True):
        path = Path(match)
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                ids.add(stripped)
    return ids


def _parse_junit(path: Path) -> dict[str, AttemptsRecord]:
    # Preserve first-seen order so the output is deterministic.
    per_test: dict[str, AttemptsRecord] = {}
    tree = ET.parse(path)
    for testcase in tree.iter("testcase"):
        name = testcase.get("name")
        if name is None:
            continue
        outcome = _testcase_outcome(testcase)
        entry = per_test.get(name)
        if entry is None:
            entry = AttemptsRecord(name=name)
            per_test[name] = entry
        entry.record(outcome=outcome)
    return per_test


def _testcase_outcome(testcase: ET.Element) -> TestStatus:
    has_failure = False
    has_error = False
    has_skipped = False
    for child in testcase:
        tag = child.tag
        if tag == "failure":
            has_failure = True
        elif tag == "error":
            has_error = True
        elif tag == "skipped":
            has_skipped = True
    if has_failure:
        return TestStatus.FAILED
    if has_error:
        return TestStatus.ERROR
    if has_skipped:
        return TestStatus.SKIPPED
    return TestStatus.PASSED


def _render_markdown(
    per_test: Mapping[str, AttemptsRecord],
    flaky_ids: AbstractSet[str],
    heading: str,
) -> str:
    total_tests = len(per_test)
    total_attempts = sum(t.attempts for t in per_test.values())
    retried = sum(1 for t in per_test.values() if t.attempts > 1)
    marked_flaky = sum(1 for name in per_test if name in flaky_ids)
    failed = sum(1 for t in per_test.values() if t.final_status in (TestStatus.FAILED, TestStatus.ERROR))
    flaky_recovered = sum(1 for t in per_test.values() if t.final_status is TestStatus.FLAKY_RECOVERED)

    lines: list[str] = []
    lines.append(f"## {heading}")
    lines.append("")
    lines.append(f"- Unique tests: **{total_tests}**")
    lines.append(f"- Total attempts: **{total_attempts}**")
    lines.append(f"- Tests with >1 attempt: **{retried}**")
    lines.append(f"- Tests marked `@pytest.mark.flaky`: **{marked_flaky}**")
    lines.append(f"- Flaky-recovered (failed then passed): **{flaky_recovered}**")
    lines.append(f"- Failing (final): **{failed}**")
    lines.append("")

    notable_final_statuses = (TestStatus.FAILED, TestStatus.ERROR, TestStatus.FLAKY_RECOVERED)
    interesting = [
        t
        for t in per_test.values()
        if t.attempts > 1 or t.name in flaky_ids or t.final_status in notable_final_statuses
    ]
    if not interesting:
        lines.append("_No retries, failures, or flaky-marked tests in this run._")
        lines.append("")
        return "\n".join(lines)

    interesting.sort(key=lambda t: (_STATUS_ORDER[t.final_status], -t.attempts, t.name))

    lines.append("| Test | Final | Attempts | Retries | Pass | Fail+Err | Skip | `@flaky` |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | :---: |")
    for t in interesting:
        marked = "yes" if t.name in flaky_ids else "no"
        fail_err = t.failed + t.errors
        lines.append(
            "| `{name}` | {status} | {attempts} | {retries} | {passed} | {fail_err} | {skipped} | {marked} |".format(
                name=t.name,
                status=t.final_status,
                attempts=t.attempts,
                retries=t.retries,
                passed=t.passed,
                fail_err=fail_err,
                skipped=t.skipped,
                marked=marked,
            )
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
