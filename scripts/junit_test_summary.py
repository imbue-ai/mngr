#!/usr/bin/env python3
"""Generate a markdown summary of an offload junit.xml with per-test run counts.

Each unique testcase in junit.xml may appear multiple times (one `<testcase>`
element per attempt) because offload runs tests multiple times -- either as
explicit retries of flaky-marked tests or because of its retry_count settings.
This script aggregates those attempts per test and emits a GitHub-flavored
markdown table listing EVERY test with:

  - its run count (how many times the test executed)
  - its final status across all runs
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
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from enum import StrEnum
from pathlib import Path
from typing import Final


class RunStatus(StrEnum):
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


# Sort order for the rendered table: surface problem tests first so that if the
# output is truncated by --max-chars, the most important rows remain visible.
_STATUS_ORDER: Final[dict[RunStatus, int]] = {
    RunStatus.FAILED: 0,
    RunStatus.ERROR: 1,
    RunStatus.FLAKY_RECOVERED: 2,
    RunStatus.PASSED: 3,
    RunStatus.SKIPPED: 4,
    RunStatus.UNKNOWN: 5,
}

# Default cap for the rendered output (in characters). GitHub's check_runs
# output.summary field accepts up to 65_535 characters, so we stay safely
# under that limit. Callers that target $GITHUB_STEP_SUMMARY (1 MiB limit)
# can pass a larger cap.
_DEFAULT_MAX_CHARS: Final[int] = 60_000


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

    def record(self, outcome: RunStatus) -> None:
        self.attempts += 1
        if outcome is RunStatus.PASSED:
            self.passed += 1
        elif outcome is RunStatus.FAILED:
            self.failed += 1
        elif outcome is RunStatus.ERROR:
            self.errors += 1
        elif outcome is RunStatus.SKIPPED:
            self.skipped += 1

    @property
    def final_status(self) -> RunStatus:
        # A test counts as passed overall if any attempt passed. If it also had
        # failures, highlight that it was flaky-recovered. A test with only
        # skips is reported as skipped (never counted as passed).
        if self.passed > 0:
            if self.failed > 0 or self.errors > 0:
                return RunStatus.FLAKY_RECOVERED
            return RunStatus.PASSED
        if self.failed > 0:
            return RunStatus.FAILED
        if self.errors > 0:
            return RunStatus.ERROR
        if self.skipped > 0:
            return RunStatus.SKIPPED
        return RunStatus.UNKNOWN


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
    parser.add_argument(
        "--max-chars",
        type=int,
        default=_DEFAULT_MAX_CHARS,
        help=(
            "Maximum characters in the rendered markdown. If the full table "
            f"exceeds this, trailing rows are omitted (default: {_DEFAULT_MAX_CHARS})."
        ),
    )
    args = parser.parse_args()

    if not args.junit.is_file():
        print(f"junit file not found: {args.junit}", file=sys.stderr)
        return 1

    flaky_ids = _load_flaky_manifest(args.flaky_manifest_glob)
    per_test = _parse_junit(args.junit)
    markdown = _render_markdown(
        per_test=per_test,
        flaky_ids=flaky_ids,
        heading=args.heading,
        max_chars=args.max_chars,
    )

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


def _testcase_outcome(testcase: ET.Element) -> RunStatus:
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
        return RunStatus.FAILED
    if has_error:
        return RunStatus.ERROR
    if has_skipped:
        return RunStatus.SKIPPED
    return RunStatus.PASSED


def _render_markdown(
    per_test: Mapping[str, AttemptsRecord],
    flaky_ids: AbstractSet[str],
    heading: str,
    max_chars: int,
) -> str:
    total_tests = len(per_test)
    total_attempts = sum(t.attempts for t in per_test.values())
    retried = sum(1 for t in per_test.values() if t.attempts > 1)
    marked_flaky = sum(1 for name in per_test if name in flaky_ids)
    failed = sum(1 for t in per_test.values() if t.final_status in (RunStatus.FAILED, RunStatus.ERROR))
    flaky_recovered = sum(1 for t in per_test.values() if t.final_status is RunStatus.FLAKY_RECOVERED)

    header_lines: list[str] = [
        f"## {heading}",
        "",
        f"- Unique tests: **{total_tests}**",
        f"- Total runs (attempts across retries): **{total_attempts}**",
        f"- Tests that ran more than once: **{retried}**",
        f"- Tests marked `@pytest.mark.flaky`: **{marked_flaky}**",
        f"- Flaky-recovered (failed then passed): **{flaky_recovered}**",
        f"- Failing (final): **{failed}**",
        "",
    ]

    if total_tests == 0:
        return "\n".join(header_lines + ["_No tests recorded in junit.xml._", ""])

    table_header = [
        "| Test | Runs | Final | `@flaky` |",
        "| --- | ---: | --- | :---: |",
    ]

    tests = sorted(per_test.values(), key=lambda t: (_STATUS_ORDER[t.final_status], -t.attempts, t.name))
    rows: list[str] = []
    for t in tests:
        marked = "yes" if t.name in flaky_ids else "no"
        rows.append(f"| `{t.name}` | {t.attempts} | {t.final_status} | {marked} |")

    return _assemble_with_truncation(
        header_lines=header_lines,
        table_header=table_header,
        rows=rows,
        max_chars=max_chars,
    )


def _assemble_with_truncation(
    header_lines: Sequence[str],
    table_header: Sequence[str],
    rows: Sequence[str],
    max_chars: int,
) -> str:
    """Join header + table, dropping trailing rows if the total exceeds max_chars.

    When truncation kicks in, appends a footer disclosing how many rows were
    omitted so the reader knows the table is not complete. If every row fits
    within max_chars without a footer, all rows are kept and no footer is added.
    """
    fixed = "\n".join([*header_lines, *table_header]) + "\n"
    # +1 per row for the newline that separates/terminates it in the body.
    full_body_size = sum(len(row) + 1 for row in rows)

    # Fast path: the full body fits under max_chars without any footer.
    if len(fixed) + full_body_size <= max_chars:
        body = "\n".join(rows) + ("\n" if rows else "")
        return fixed + body

    # Truncation path: reserve headroom for the footer so we do not emit rows
    # we will later have to drop again once the footer is appended. `max(0, ...)`
    # keeps the bound non-negative so the loop is well-defined when `max_chars`
    # is smaller than the fixed header + footer overhead. In that degenerate
    # case no rows are kept; the caller still gets the stats header + footer,
    # which may slightly exceed `max_chars` -- but there is nothing further we
    # can drop without losing the stats themselves.
    footer_headroom = 160
    budget = max(0, max_chars - len(fixed) - footer_headroom)
    kept_rows: list[str] = []
    used = 0
    for row in rows:
        row_len = len(row) + 1  # +1 for newline
        if used + row_len > budget:
            break
        kept_rows.append(row)
        used += row_len

    body = "\n".join(kept_rows) + ("\n" if kept_rows else "")
    omitted = len(rows) - len(kept_rows)
    footer = (
        f"\n_... {omitted} additional test row(s) omitted to keep the summary under "
        f"{max_chars} characters. See the workflow run page for the full list._\n"
    )
    return fixed + body + footer


if __name__ == "__main__":
    sys.exit(main())
