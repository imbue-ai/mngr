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
import html
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
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

# Per-failure body cap so a single huge traceback can't crowd out everything
# else in the summary. The full body is still available on the workflow run
# page; this just keeps the PR-checks render readable.
_PER_FAILURE_BODY_CAP: Final[int] = 2_000

# Cap on the inline summary line for a failure (the first line of the
# failure message attribute, surfaced in the <details>/<summary> header).
_FAILURE_MESSAGE_LINE_CAP: Final[int] = 200


@dataclass(frozen=True)
class FailureDetail:
    """A single failed/errored testcase attempt and its captured body.

    `body` is the inner text of the `<failure>`/`<error>` element (typically
    the pytest traceback). `message` is the element's `message` attribute
    (typically the exception's repr). `kind` is "failure" or "error".
    `attempt` is the 1-based index of this attempt within the test's run
    sequence, used to label failures of retried tests as "attempt N/M".
    """

    name: str
    kind: str
    message: str
    body: str
    attempt: int


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
            "Maximum characters in the rendered markdown. If the rendered output "
            "(stats header + per-failure detail blocks + per-test table) exceeds "
            "this, trailing failure blocks and/or trailing table rows are dropped "
            f"and a footer discloses the omissions (default: {_DEFAULT_MAX_CHARS})."
        ),
    )
    args = parser.parse_args()

    if not args.junit.is_file():
        print(f"junit file not found: {args.junit}", file=sys.stderr)
        return 1

    flaky_ids = _load_flaky_manifest(args.flaky_manifest_glob)
    per_test, failures = _parse_junit(args.junit)
    markdown = _render_markdown(
        per_test=per_test,
        failures=failures,
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


def _parse_junit(path: Path) -> tuple[dict[str, AttemptsRecord], list[FailureDetail]]:
    """Parse junit.xml into per-test attempts and per-attempt failure detail.

    A failed/errored attempt contributes both an `AttemptsRecord` count and a
    `FailureDetail` carrying the captured traceback so the renderer can show
    the actual error inline. A single testcase can carry both `<failure>` and
    `<error>` children; we record each so nothing is silently dropped.
    """
    # Preserve first-seen order so the output is deterministic.
    per_test: dict[str, AttemptsRecord] = {}
    failures: list[FailureDetail] = []
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
        # `entry.attempts` was just incremented, so it is the 1-based index of
        # this attempt within the test's run sequence.
        attempt_index = entry.attempts
        for child in testcase:
            if child.tag in ("failure", "error"):
                failures.append(
                    FailureDetail(
                        name=name,
                        kind=child.tag,
                        message=child.get("message") or "",
                        body=(child.text or "").strip(),
                        attempt=attempt_index,
                    )
                )
    return per_test, failures


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


def _final_status_cell(t: AttemptsRecord) -> str:
    """Render the `Final` column for a single test.

    For flaky-recovered tests, expand the bare "flaky-recovered" label into
    `flaked N, passed M` so the reader can see how many attempts failed before
    one passed. Other statuses render as the plain enum string.
    """
    status = t.final_status
    if status is RunStatus.FLAKY_RECOVERED:
        return f"flaked {t.failed + t.errors}, passed {t.passed}"
    return str(status)


def _render_markdown(
    per_test: Mapping[str, AttemptsRecord],
    failures: Sequence[FailureDetail],
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
        rows.append(f"| `{t.name}` | {t.attempts} | {_final_status_cell(t)} | {marked} |")

    failure_blocks = [_render_failure_block(f, total_attempts=per_test[f.name].attempts) for f in failures]

    return _assemble_with_truncation(
        header_lines=header_lines,
        failure_blocks=failure_blocks,
        table_header=table_header,
        rows=rows,
        max_chars=max_chars,
    )


def _render_failure_block(failure: FailureDetail, total_attempts: int) -> str:
    """Render one failed/errored attempt as a collapsed `<details>` block.

    Shows the test id, attempt label (when the test ran more than once),
    kind (failure/error), and the first line of the failure message in the
    summary header so the reader can scan without expanding. The body is
    the captured traceback, capped at `_PER_FAILURE_BODY_CAP` characters
    so a single huge traceback can't crowd out everything else.

    For tests that ran more than once (retries / flaky-recovered), each
    failed attempt is labelled "attempt N/M" so a reader can tell whether
    a test failed only on its first try (likely flake) vs every attempt.
    """
    body = failure.body
    if len(body) > _PER_FAILURE_BODY_CAP:
        body = body[:_PER_FAILURE_BODY_CAP] + f"\n... [truncated to {_PER_FAILURE_BODY_CAP} chars]"
    first_line = failure.message.splitlines()[0] if failure.message else ""
    if len(first_line) > _FAILURE_MESSAGE_LINE_CAP:
        first_line = first_line[:_FAILURE_MESSAGE_LINE_CAP] + "..."
    attempt_label = f" (attempt {failure.attempt}/{total_attempts})" if total_attempts > 1 else ""
    summary = f"<code>{html.escape(failure.name)}</code>{attempt_label} &mdash; {failure.kind}"
    if first_line:
        summary += f": {html.escape(first_line)}"
    # The blank line after <summary> is required by GitHub-flavored markdown
    # for the fenced code block inside <details> to render as code.
    return f"<details><summary>{summary}</summary>\n\n```\n{body}\n```\n\n</details>"


def _assemble_with_truncation(
    header_lines: Sequence[str],
    failure_blocks: Sequence[str],
    table_header: Sequence[str],
    rows: Sequence[str],
    max_chars: int,
) -> str:
    """Join header + failure detail blocks + table, dropping trailing items if needed.

    Layout: stats header, then the per-failure `<details>` blocks (so readers
    see actual error output first), then the per-test runs/retries table.

    Failures take priority over table rows: if everything won't fit under
    `max_chars`, we keep as many failure blocks as fit and then fill the
    remainder with table rows. A footer discloses how many of each were
    omitted. If the full body fits, no footer is emitted.
    """
    stats = "\n".join(header_lines) + "\n"
    failures_heading = "## Failures\n\n" if failure_blocks else ""
    table_head = "\n".join(table_header) + "\n"

    # +2 between failure blocks for the blank line separator; +1 per row for
    # its terminating newline.
    failure_size = sum(len(b) + 2 for b in failure_blocks)
    rows_size = sum(len(r) + 1 for r in rows)
    fixed_size = len(stats) + len(failures_heading) + len(table_head)

    # Fast path: the whole document fits under max_chars without a footer.
    if fixed_size + failure_size + rows_size <= max_chars:
        failures_body = ("\n\n".join(failure_blocks) + "\n\n") if failure_blocks else ""
        rows_body = ("\n".join(rows) + "\n") if rows else ""
        return stats + failures_heading + failures_body + table_head + rows_body

    # Truncation path. Reserve headroom for the footer so we do not emit
    # items we will later have to drop again once the footer is appended.
    # `max(0, ...)` keeps the bound non-negative if the fixed overhead alone
    # already exceeds max_chars; in that degenerate case we keep nothing
    # droppable and the result may slightly exceed max_chars (the stats
    # header + footer is what we owe the caller -- there is nothing else
    # we can cut without losing the stats themselves).
    footer_headroom = 240
    budget = max(0, max_chars - fixed_size - footer_headroom)

    kept_failures: list[str] = []
    used = 0
    for block in failure_blocks:
        cost = len(block) + 2
        if used + cost > budget:
            break
        kept_failures.append(block)
        used += cost

    kept_rows: list[str] = []
    for row in rows:
        cost = len(row) + 1
        if used + cost > budget:
            break
        kept_rows.append(row)
        used += cost

    failures_body = ("\n\n".join(kept_failures) + "\n\n") if kept_failures else ""
    rows_body = ("\n".join(kept_rows) + "\n") if kept_rows else ""

    omitted_failures = len(failure_blocks) - len(kept_failures)
    omitted_rows = len(rows) - len(kept_rows)
    notes: list[str] = []
    if omitted_failures:
        notes.append(f"{omitted_failures} failure detail block(s) omitted")
    if omitted_rows:
        notes.append(f"{omitted_rows} additional test row(s) omitted")
    footer = (
        f"\n_... {' and '.join(notes)} to keep the summary under "
        f"{max_chars} characters. See the workflow run page for the full list._\n"
    )
    return stats + failures_heading + failures_body + table_head + rows_body + footer


if __name__ == "__main__":
    sys.exit(main())
