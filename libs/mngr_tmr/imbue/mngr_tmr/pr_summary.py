"""Build the human-readable part of the TMR pull request description.

The reducer agent opens the run's PR itself, and its body should carry the
headline findings rather than just a link to the report. Tabulating 80 mapper
outcomes is exactly the kind of work an agent does slowly and inaccurately, so
the reducer shells out to this module instead: it reads the same outcome JSON
files the reducer already has under its inputs directory and emits finished
markdown on stdout.

Usage (from the reducer prompt)::

    python -m imbue.mngr_tmr.pr_summary <inputs_dir>

Layout of ``inputs_dir`` matches what the orchestrator rsyncs to the reducer:
``<inputs_dir>/<agent_name>/test_output/testing_agent_outcome.json``.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from imbue.mngr.primitives import AgentName
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.report import EXTRACTED_TEST_OUTPUT_DIR
from imbue.mngr_tmr.report import Escalation
from imbue.mngr_tmr.report import EscalationKind
from imbue.mngr_tmr.report import ReportSection
from imbue.mngr_tmr.report import TestMapReduceResult
from imbue.mngr_tmr.report import escalation_kind_label
from imbue.mngr_tmr.report import load_integrator_outcome_file
from imbue.mngr_tmr.report import load_testing_agent_outcome
from imbue.mngr_tmr.report import report_section_of
from imbue.mngr_tmr.report import section_label

# Order the status breakdown reads in: the sections a reviewer most needs to act
# on come first, and the uneventful ones last.
_BREAKDOWN_ORDER: list[ReportSection] = [
    ReportSection.IMPL_FIXES,
    ReportSection.NON_IMPL_FIXES,
    ReportSection.UNRESOLVED,
    ReportSection.FAILED,
    ReportSection.CLEAN_PASS,
    ReportSection.RUNNING,
]

_ESCALATION_KIND_ORDER: list[EscalationKind] = [EscalationKind.BLOCKER, EscalationKind.SHARED_PATTERN]


def _escape_cell(text: str) -> str:
    """Make a string safe to place in a markdown table cell.

    Pipes would split the cell and newlines would end the row, so both are
    neutralized. Escalation detail is markdown written by an agent, so it can
    contain either.
    """
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _first_line(text: str) -> str:
    """Return the first non-empty line of a markdown blob."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def collect_results(inputs_dir: Path) -> list[TestMapReduceResult]:
    """Read every mapper outcome under ``inputs_dir`` into a result row.

    Agent directories with no readable outcome file are skipped: those mappers
    never published, which the orchestrator already reports separately.
    """
    results: list[TestMapReduceResult] = []
    if not inputs_dir.is_dir():
        return results
    for agent_dir in sorted(inputs_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        # Only directories that actually hold an outcome are agent directories.
        # The inputs directory sits inside the reducer's work_dir, so it can
        # also contain unrelated entries (dotfiles, tool state) whose names are
        # not valid agent names at all.
        if not (agent_dir / EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME).is_file():
            continue
        agent_name = AgentName(agent_dir.name)
        outcome = load_testing_agent_outcome(agent_name, inputs_dir)
        if outcome is None:
            continue
        results.append(
            TestMapReduceResult(
                # The agent name is the only per-test identity available here;
                # the pytest node id lives in orchestrator-side metadata, which
                # the reducer does not receive.
                test_node_id=agent_dir.name,
                agent_name=agent_name,
                changes=outcome.changes,
                errored=outcome.errored,
                tests_passing_before=outcome.tests_passing_before,
                tests_passing_after=outcome.tests_passing_after,
                summary_markdown=outcome.summary_markdown,
                test_runs=outcome.test_runs,
                escalations=outcome.escalations,
            )
        )
    return results


def build_status_breakdown(results: list[TestMapReduceResult]) -> str:
    """Render the per-status mapper counts as a markdown table."""
    counts = Counter(report_section_of(result) for result in results)
    lines = [
        f"### Mapper outcomes ({len(results)} total)",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for section in _BREAKDOWN_ORDER:
        count = counts.get(section, 0)
        if count:
            lines.append(f"| {section_label(section)} | {count} |")
    return "\n".join(lines)


def build_escalations_table(
    results: list[TestMapReduceResult],
    reducer_escalations: tuple[Escalation, ...] = (),
) -> str:
    """Render every escalation as a markdown table, blockers first.

    Covers both the test agents' escalations and the reducer's own -- the latter
    include the repeated-change groups it found, which are the most actionable
    thing the run produces and must not be omitted from the PR.
    """
    rows: list[tuple[EscalationKind, str, str, str]] = []
    for result in results:
        for escalation in result.escalations:
            rows.append(
                (
                    escalation.kind,
                    str(result.agent_name),
                    escalation.title,
                    _first_line(escalation.detail_markdown),
                )
            )
    for escalation in reducer_escalations:
        rows.append((escalation.kind, "integrator", escalation.title, _first_line(escalation.detail_markdown)))
    if not rows:
        return "### Escalations\n\nNone reported."

    rows.sort(key=lambda row: (_ESCALATION_KIND_ORDER.index(row[0]), row[1]))
    lines = [
        f"### Escalations ({len(rows)})",
        "",
        "| Kind | Source | Title | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for kind, source, title, detail in rows:
        lines.append(
            f"| {escalation_kind_label(kind)} | `{_escape_cell(source)}` "
            f"| {_escape_cell(title)} | {_escape_cell(detail)} |"
        )
    return "\n".join(lines)


def build_pr_summary(inputs_dir: Path, reducer_outcome_path: Path | None = None) -> str:
    """Build the full markdown summary block for the PR description.

    ``reducer_outcome_path`` is the integrator's own outcome file, when it has
    already been written; its escalations join the table.
    """
    results = collect_results(inputs_dir)
    reducer_escalations: tuple[Escalation, ...] = ()
    if reducer_outcome_path is not None:
        reducer_outcome = load_integrator_outcome_file(reducer_outcome_path)
        if reducer_outcome is not None:
            reducer_escalations = reducer_outcome.escalations
    return "\n\n".join([build_status_breakdown(results), build_escalations_table(results, reducer_escalations)])


def _format_run_date(run_name: str) -> str:
    """Render the leading YYYYMMDD of a run name as YYYY-MM-DD.

    Run names are UTC YYYYMMDDHHMMSS timestamps generated by the framework. A
    name that does not follow that shape is passed through unchanged rather
    than guessed at.
    """
    if len(run_name) >= 8 and run_name[:8].isdigit():
        return f"{run_name[:4]}-{run_name[4:6]}-{run_name[6:8]}"
    return run_name


def build_pr_title(branch_name: str, results: list[TestMapReduceResult]) -> str:
    """Build the PR title: mechanical, but readable at a glance in a PR list.

    ``branch_name`` is the reducer's own branch, ``<variant>/<run>/reducer``,
    which is where the variant and run name come from.
    """
    parts = branch_name.split("/")
    variant = parts[0] if parts else branch_name
    run_name = parts[1] if len(parts) > 1 else ""

    counts = Counter(report_section_of(result) for result in results)
    fixes = counts.get(ReportSection.IMPL_FIXES, 0) + counts.get(ReportSection.NON_IMPL_FIXES, 0)
    unresolved = counts.get(ReportSection.UNRESOLVED, 0) + counts.get(ReportSection.FAILED, 0)
    escalations = sum(len(result.escalations) for result in results)

    # Only non-zero facts earn a place, so the common "everything was clean"
    # run gets a short title instead of a row of zeros.
    summary_bits = []
    if fixes:
        summary_bits.append(f"{fixes} fixed")
    if unresolved:
        summary_bits.append(f"{unresolved} unresolved")
    if escalations:
        summary_bits.append(f"{escalations} escalated")
    if not summary_bits:
        summary_bits.append(f"{len(results)} tests clean")

    date_part = f" {_format_run_date(run_name)}" if run_name else ""
    return f"TMR {variant}{date_part}: {', '.join(summary_bits)}"


def main(argv: list[str]) -> int:
    """Print either the PR body summary or the PR title."""
    parser = argparse.ArgumentParser(description="Build TMR pull request title/body from mapper outcomes.")
    parser.add_argument("inputs_dir", type=Path, help="Directory of per-mapper output directories")
    parser.add_argument(
        "--reducer-outcome",
        type=Path,
        help="Path to the integrator's own outcome file, so its escalations join the table",
    )
    parser.add_argument(
        "--title",
        metavar="BRANCH",
        help="Print the PR title for this reducer branch instead of the body summary",
    )
    args = parser.parse_args(argv[1:])

    if args.title:
        print(build_pr_title(args.title, collect_results(args.inputs_dir)))
    else:
        print(build_pr_summary(args.inputs_dir, args.reducer_outcome))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
