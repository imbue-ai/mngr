"""HTML report generation for the test-mapreduce plugin.

The reporter takes a list of ``AgentMetadata`` from orchestration and
reads each agent's outcome JSON from ``output_dir/<agent_name>/`` on
demand. Outcome JSON shape is a contract between the agents and this
module; orchestration does not parse it. Parsed outcomes are cached
in-process (test-agent outcomes and the integrator outcome are
immutable once an agent has published them, so caching is safe).
"""

import html
import json
from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from markdown_it import MarkdownIt

from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.detail_renderer import ASCIINEMA_PLAYER_CSS
from imbue.mngr.utils.detail_renderer import ASCIINEMA_PLAYER_JS
from imbue.mngr.utils.detail_renderer import DETAIL_CSS
from imbue.mngr.utils.detail_renderer import render_test_detail
from imbue.mngr_tmr.data_types import AgentKind
from imbue.mngr_tmr.data_types import AgentMetadata
from imbue.mngr_tmr.data_types import Change
from imbue.mngr_tmr.data_types import ChangeKind
from imbue.mngr_tmr.data_types import ChangeStatus
from imbue.mngr_tmr.data_types import IntegratorResult
from imbue.mngr_tmr.data_types import ReportSection
from imbue.mngr_tmr.data_types import TestMapReduceResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.data_types import TestRunInfo
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME

_EXTRACTED_TEST_OUTPUT_DIR = "test_output"

# Outcome JSON for a given agent is immutable once present. Cache keyed by
# agent_name so generate_html_report can be called many times during polling
# without re-parsing.
_TESTING_OUTCOME_CACHE: dict[AgentName, TestResult] = {}
_INTEGRATOR_OUTCOME_CACHE: dict[AgentName, IntegratorResult] = {}

_SECTION_ORDER: list[ReportSection] = [
    ReportSection.NON_IMPL_FIXES,
    ReportSection.IMPL_FIXES,
    ReportSection.BLOCKED,
    ReportSection.FAILED,
    ReportSection.CLEAN_PASS,
    ReportSection.RUNNING,
]

_SECTION_LABELS: dict[ReportSection, str] = {
    ReportSection.NON_IMPL_FIXES: "Non-implementation fixes",
    ReportSection.IMPL_FIXES: "Implementation fixes",
    ReportSection.BLOCKED: "Blocked",
    ReportSection.FAILED: "Failed",
    ReportSection.CLEAN_PASS: "Clean pass",
    ReportSection.RUNNING: "Running",
}

_SECTION_COLORS: dict[ReportSection, str] = {
    ReportSection.NON_IMPL_FIXES: "rgb(33, 150, 243)",
    ReportSection.IMPL_FIXES: "rgb(76, 175, 80)",
    ReportSection.BLOCKED: "rgb(244, 67, 54)",
    ReportSection.FAILED: "rgb(255, 152, 0)",
    ReportSection.CLEAN_PASS: "rgb(158, 158, 158)",
    ReportSection.RUNNING: "rgb(3, 169, 244)",
}

_md = MarkdownIt()

_NON_IMPL_CHANGE_KINDS = frozenset({ChangeKind.FIX_TEST, ChangeKind.IMPROVE_TEST, ChangeKind.FIX_TUTORIAL})


def _parse_outcome_json(raw: str) -> TestResult:
    """Parse an outcome JSON string into a TestResult.

    Raises json.JSONDecodeError, KeyError, or ValueError on invalid data.
    """
    data = json.loads(raw)
    raw_changes = data.get("changes", {})
    changes: dict[ChangeKind, Change] = {
        ChangeKind(kind_str): Change(
            status=ChangeStatus(entry["status"]),
            summary_markdown=entry.get("summary_markdown", entry.get("summary", "")),
        )
        for kind_str, entry in raw_changes.items()
    }
    raw_runs = data.get("test_runs", [])
    test_runs = tuple(
        TestRunInfo(
            run_name=run_entry.get("run_name", ""),
            description_markdown=run_entry.get("description_markdown", ""),
        )
        for run_entry in raw_runs
    )
    return TestResult(
        changes=changes,
        errored=data.get("errored", False),
        tests_passing_before=data.get("tests_passing_before"),
        tests_passing_after=data.get("tests_passing_after"),
        summary_markdown=data.get("summary_markdown", ""),
        test_runs=test_runs,
    )


def _outcome_path_for_testing_agent(output_dir: Path, agent_name: AgentName) -> Path:
    return output_dir / str(agent_name) / _EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME


def _outcome_path_for_integrator(output_dir: Path, agent_name: AgentName) -> Path:
    return output_dir / str(agent_name) / _EXTRACTED_TEST_OUTPUT_DIR / INTEGRATOR_OUTCOME_FILENAME


def _load_testing_agent_outcome(agent_name: AgentName, output_dir: Path) -> TestResult | None:
    """Read and cache a testing agent's outcome from the extracted output dir."""
    cached = _TESTING_OUTCOME_CACHE.get(agent_name)
    if cached is not None:
        return cached
    path = _outcome_path_for_testing_agent(output_dir, agent_name)
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        outcome = _parse_outcome_json(raw)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("Failed to parse outcome for agent '{}': {}", agent_name, exc)
        return None
    _TESTING_OUTCOME_CACHE[agent_name] = outcome
    return outcome


def _load_integrator_outcome(meta: AgentMetadata, output_dir: Path) -> IntegratorResult:
    """Read and cache the integrator's outcome, returning an empty result on miss."""
    empty = IntegratorResult(agent_name=meta.agent_name, branch_name=meta.branch_name)
    cached = _INTEGRATOR_OUTCOME_CACHE.get(meta.agent_name)
    if cached is not None:
        return cached
    path = _outcome_path_for_integrator(output_dir, meta.agent_name)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read integrator outcome for '{}': {}", meta.agent_name, exc)
        return empty
    result = IntegratorResult(
        agent_name=meta.agent_name,
        squashed_branches=tuple(data.get("squashed_branches", ())),
        squashed_commit_hash=data.get("squashed_commit_hash"),
        impl_priority=tuple(data.get("impl_priority", ())),
        impl_commit_hashes=data.get("impl_commit_hashes", {}),
        failed=tuple(data.get("failed", ())),
        branch_name=meta.branch_name,
    )
    _INTEGRATOR_OUTCOME_CACHE[meta.agent_name] = result
    return result


def _row_from_metadata(meta: AgentMetadata, outcome: TestResult | None) -> TestMapReduceResult:
    """Build a renderable row from per-agent metadata + optional parsed outcome."""
    if meta.error_summary is not None:
        return TestMapReduceResult(
            test_node_id=meta.test_node_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            errored=True,
            summary_markdown=meta.error_summary,
            branch_name=meta.branch_name,
        )
    if outcome is None:
        return TestMapReduceResult(
            test_node_id=meta.test_node_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            summary_markdown="Agent is still running...",
            branch_name=meta.branch_name,
        )
    return TestMapReduceResult(
        test_node_id=meta.test_node_id or str(meta.agent_name),
        agent_name=meta.agent_name,
        changes=outcome.changes,
        errored=outcome.errored,
        tests_passing_before=outcome.tests_passing_before,
        tests_passing_after=outcome.tests_passing_after,
        summary_markdown=outcome.summary_markdown,
        branch_name=meta.branch_name,
        test_runs=outcome.test_runs,
    )


def _build_rows(agents: Sequence[AgentMetadata], output_dir: Path) -> list[TestMapReduceResult]:
    """Build renderable rows for all testing agents (one per AgentMetadata).

    Skips the integrator -- it has its own panel in the report, not a row.
    """
    rows: list[TestMapReduceResult] = []
    for meta in agents:
        if meta.kind is not AgentKind.TESTING_AGENT:
            continue
        outcome = _load_testing_agent_outcome(meta.agent_name, output_dir) if meta.error_summary is None else None
        rows.append(_row_from_metadata(meta, outcome))
    return rows


def _report_section_of(result: TestMapReduceResult) -> ReportSection:
    """Derive a report section from a result for report grouping/coloring.

    ``errored=True`` indicates an infrastructure failure (launch failed,
    agent timed out, details missing) and is rendered in the FAILED section.
    The BLOCKED section is reserved for results where the coding agent
    itself reported every change as BLOCKED.
    """
    if result.errored:
        return ReportSection.FAILED
    if result.tests_passing_before is None and result.tests_passing_after is None and not result.changes:
        return ReportSection.RUNNING
    if result.changes and all(c.status == ChangeStatus.BLOCKED for c in result.changes.values()):
        return ReportSection.BLOCKED
    if any(kind in _NON_IMPL_CHANGE_KINDS for kind in result.changes):
        return ReportSection.NON_IMPL_FIXES
    if ChangeKind.FIX_IMPL in result.changes:
        return ReportSection.IMPL_FIXES
    if not result.changes and result.tests_passing_after is True:
        return ReportSection.CLEAN_PASS
    return ReportSection.BLOCKED


def generate_html_report(
    agents: Sequence[AgentMetadata],
    output_dir: Path,
    *,
    integrator_metadata: AgentMetadata | None = None,
    run_commands: list[tuple[str, str]] | None = None,
) -> Path:
    """Generate an HTML report summarizing the run.

    Walks ``agents`` and reads each testing agent's outcome from
    ``output_dir/<agent_name>/test_output/``; reads the integrator's
    outcome (if any) from ``output_dir/<integrator_name>/``. Writes the
    report to ``output_dir/index.html`` and returns that path.

    Side-effect free except for writing the local file. Mirroring the
    report to s3 is the caller's responsibility (see
    ``report_upload.maybe_upload_report``); orchestration calls it from
    ``_emit_report`` so each regeneration triggers an upload.
    """
    rows = _build_rows(agents, output_dir)
    integrator = _load_integrator_outcome(integrator_metadata, output_dir) if integrator_metadata is not None else None

    counts: dict[ReportSection, int] = {}
    for r in rows:
        sec = _report_section_of(r)
        counts[sec] = counts.get(sec, 0) + 1

    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]] = {}
    for r in rows:
        try:
            runs = _find_test_artifact_runs(output_dir, r.agent_name, r.test_runs)
        except OSError as exc:
            if "Too many open files" in str(exc):
                logger.warning("FD exhaustion while scanning artifacts for '{}': {}", r.agent_name, exc)
            raise
        if runs:
            agent_artifact_runs[str(r.agent_name)] = runs

    toc_html = _build_toc_sidebar(counts)
    tables_html = _build_grouped_tables(rows, agent_artifact_runs, integrator, run_commands)
    panels_html = _build_artifact_panels(agent_artifact_runs)

    has_artifacts = bool(agent_artifact_runs)
    asciinema_head = ""
    if has_artifacts:
        asciinema_head = (
            f'  <link rel="stylesheet" type="text/css" href="{ASCIINEMA_PLAYER_CSS}">\n'
            f'  <script src="{ASCIINEMA_PLAYER_JS}"></script>'
        )

    css = _html_report_css()
    artifact_css = _artifact_panel_css() if has_artifacts else ""
    artifact_js = _artifact_panel_js() if has_artifacts else ""
    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Test Map-Reduce Report</title>
{asciinema_head}
  <style>
{css}
{artifact_css}
  </style>
</head>
<body>
{toc_html}
  <div class="main-content">
    <h1>Test Map-Reduce Report</h1>
    <p class="summary">{len(rows)} test(s)</p>
{_build_run_commands_html(run_commands)}
{tables_html}
  </div>
{panels_html}
{artifact_js}
</body>
</html>
"""
    output_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("HTML report written to {}", output_path)
    return output_path


def _build_run_commands_html(commands: list[tuple[str, str]] | None) -> str:
    """Build an HTML block showing useful commands for the run."""
    if not commands:
        return ""
    items = ""
    for label, cmd in commands:
        escaped_cmd = html.escape(cmd)
        items += f'    <div class="run-cmd"><span class="run-cmd-label">{html.escape(label)}:</span> <code>{escaped_cmd}</code></div>\n'
    return f'  <div class="run-commands">\n{items}  </div>\n'


def _build_toc_sidebar(counts: dict[ReportSection, int]) -> str:
    """Build a sticky left sidebar with links to sections."""
    if not counts:
        return ""
    links = ""
    for sec in _SECTION_ORDER:
        count = counts.get(sec, 0)
        if count == 0:
            continue
        label = _SECTION_LABELS.get(sec, sec.value)
        color = _SECTION_COLORS.get(sec, "rgb(158, 158, 158)")
        anchor = f"sec-{sec.value}"
        links += f'    <a href="#{anchor}" class="toc-link" style="color: {color};">{label} ({count})</a>\n'
    return f'  <div class="toc-sidebar">\n{links}  </div>\n'


def _merged_status(result: TestMapReduceResult, integrator: IntegratorResult | None) -> str:
    """Return merged status: commit hash for impl, checkmark for squashed, X for failed."""
    if integrator is None or result.branch_name is None:
        return ""
    branch = result.branch_name
    if branch in integrator.impl_commit_hashes:
        commit_hash = html.escape(integrator.impl_commit_hashes[branch][:10])
        return f"<code>{commit_hash}</code>"
    if branch in set(integrator.squashed_branches):
        return "&#10003;"
    if branch in set(integrator.impl_priority) and branch not in integrator.impl_commit_hashes:
        return "&#10003;"
    if branch in set(integrator.failed):
        return "&#10007;"
    return ""


def _build_grouped_tables(
    results: list[TestMapReduceResult],
    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]] | None = None,
    integrator: IntegratorResult | None = None,
    run_commands: list[tuple[str, str]] | None = None,
) -> str:
    """Build HTML tables grouped by report section."""
    agent_artifact_runs = agent_artifact_runs or {}
    grouped: dict[ReportSection, list[TestMapReduceResult]] = {}
    for r in results:
        sec = _report_section_of(r)
        grouped.setdefault(sec, []).append(r)

    sections = ""
    for sec in _SECTION_ORDER:
        group = grouped.get(sec)
        if not group:
            continue
        color = _SECTION_COLORS.get(sec, "rgb(158, 158, 158)")
        label = _SECTION_LABELS.get(sec, sec.value)
        anchor = f"sec-{sec.value}"

        if sec == ReportSection.IMPL_FIXES and integrator is not None and integrator.impl_priority:
            priority_order = {branch: i for i, branch in enumerate(integrator.impl_priority)}
            group = sorted(
                group,
                key=lambda r: priority_order.get(r.branch_name or "", len(priority_order)),
            )

        is_running = sec == ReportSection.RUNNING
        sections += f'    <h2 id="{anchor}" style="color: {color};">{label} ({len(group)})</h2>\n'

        # Show resolution hint for the blocked section
        if sec == ReportSection.BLOCKED:
            reintegrate_cmd = ""
            if run_commands:
                for cmd_label, cmd_text in run_commands:
                    if "reintegrate" in cmd_label.lower():
                        reintegrate_cmd = html.escape(cmd_text)
                        break
            sections += (
                '    <div class="blocked-hint">\n'
                "      <p>To resolve issues with a blocked agent:</p>\n"
                "      <ol>\n"
                "        <li><code>mngr connect $agent_name</code></li>\n"
                '        <li>When done, tell it to "regenerate the outcome file"</li>\n'
            )
            if reintegrate_cmd:
                sections += f"        <li>Run: <code>{reintegrate_cmd}</code></li>\n"
            sections += "      </ol>\n    </div>\n"

        # Show squashed commit hash for the non-impl fixes section
        if sec == ReportSection.NON_IMPL_FIXES and integrator is not None and integrator.squashed_commit_hash:
            escaped_hash = html.escape(integrator.squashed_commit_hash[:10])
            sections += f'    <p class="squashed-hash">Squashed commit: <code>{escaped_hash}</code></p>\n'
        is_clean_pass = sec == ReportSection.CLEAN_PASS
        sections += "    <table>\n      <thead>\n        <tr>"
        if is_running:
            sections += "<th>Test</th><th>Agent</th>"
        elif is_clean_pass:
            sections += "<th>Test</th><th>Agent</th><th>Branch</th>"
            if agent_artifact_runs:
                sections += "<th>Artifacts</th>"
        else:
            sections += "<th>Test</th><th>Changes</th><th>Agent</th><th>Branch</th><th>Merged?</th>"
            if agent_artifact_runs:
                sections += "<th>Artifacts</th>"
        sections += "</tr>\n      </thead>\n      <tbody>\n"
        if is_running:
            col_count = 2
        elif is_clean_pass:
            col_count = 3 + (1 if agent_artifact_runs else 0)
        else:
            col_count = 5 + (1 if agent_artifact_runs else 0)
        for r in group:
            agent_name_str = str(r.agent_name)
            test_id_html = _format_test_id(r.test_node_id)
            if is_running:
                sections += f"""        <tr>
          <td>{test_id_html}</td>
          <td><code>{html.escape(agent_name_str)}</code></td>
        </tr>
"""
            elif is_clean_pass:
                branch_cell = r.branch_name if r.branch_name else "-"
                artifact_cell = ""
                if agent_artifact_runs:
                    if agent_name_str in agent_artifact_runs:
                        escaped = html.escape(agent_name_str)
                        artifact_cell = f'<td><button class="artifacts-btn" data-agent="{escaped}">View</button></td>'
                    else:
                        artifact_cell = "<td>-</td>"
                sections += f"""        <tr>
          <td>{test_id_html}</td>
          <td><code>{html.escape(agent_name_str)}</code></td>
          <td><code>{html.escape(branch_cell)}</code></td>
          {artifact_cell}
        </tr>
"""
            else:
                branch_cell = r.branch_name if r.branch_name else "-"
                changes_cell = _format_changes(r.changes) if r.changes else "-"
                merged_cell = _merged_status(r, integrator)
                artifact_cell = ""
                if agent_artifact_runs:
                    if agent_name_str in agent_artifact_runs:
                        escaped = html.escape(agent_name_str)
                        artifact_cell = f'<td><button class="artifacts-btn" data-agent="{escaped}">View</button></td>'
                    else:
                        artifact_cell = "<td>-</td>"
                sections += f"""        <tr>
          <td>{test_id_html}</td>
          <td>{changes_cell}</td>
          <td><code>{html.escape(agent_name_str)}</code></td>
          <td><code>{html.escape(branch_cell)}</code></td>
          <td>{merged_cell}</td>
          {artifact_cell}
        </tr>
"""
            if r.summary_markdown and not is_running:
                summary_html = _render_markdown(r.summary_markdown)
                sections += f'        <tr class="summary-row"><td colspan="{col_count}" class="md summary-cell">{summary_html}</td></tr>\n'
        sections += "      </tbody>\n    </table>\n"

    return sections


_CHANGE_STATUS_ICONS: dict[ChangeStatus, str] = {
    ChangeStatus.SUCCEEDED: "&#10003;",
    ChangeStatus.FAILED: "&#10007;",
    ChangeStatus.BLOCKED: "&#9644;",
}


def _format_test_id(test_node_id: str) -> str:
    """Format a test node ID with soft line breaks after :: separators."""
    return html.escape(test_node_id).replace("::", "::<wbr>")


def _format_changes(changes: dict[ChangeKind, Change]) -> str:
    """Format changes as concise kind + icon pairs."""
    parts = []
    for kind, change in changes.items():
        icon = _CHANGE_STATUS_ICONS.get(change.status, "?")
        parts.append(f"{kind.value} {icon}")
    return ", ".join(parts)


def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return _md.render(text)


def _html_report_css() -> str:
    """Return the CSS stylesheet for the HTML report.

    Uses rgb() colors instead of hex to avoid ratchet false positives.
    """
    return (
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; }\n"
        "    h1 { color: rgb(51, 51, 51); }\n"
        "    h2 { margin-top: 1.5rem; font-size: 1.1rem; }\n"
        "    .summary { margin-bottom: 0.5rem; color: rgb(102, 102, 102); }\n"
        "    .run-commands { background: rgb(245, 245, 245); border-radius: 6px; padding: 0.75rem 1rem;"
        " margin-bottom: 1.5rem; font-size: 0.85rem; }\n"
        "    .run-cmd { margin: 0.3rem 0; }\n"
        "    .run-cmd-label { color: rgb(80, 80, 80); }\n"
        "    .blocked-hint { background: rgb(255, 243, 224); border-left: 3px solid rgb(244, 67, 54);"
        " padding: 0.5rem 1rem; margin-bottom: 1rem; font-size: 0.9rem; border-radius: 0 4px 4px 0; }\n"
        "    .blocked-hint p { margin: 0 0 0.3rem 0; font-weight: 600; }\n"
        "    .blocked-hint ol { margin: 0; padding-left: 1.5rem; }\n"
        "    .toc-sidebar { position: sticky; top: 2rem; width: 200px; float: left;"
        " padding-right: 1rem; }\n"
        "    .toc-link { display: block; font-weight: 600; font-size: 0.9rem;"
        " text-decoration: none; margin-bottom: 0.5rem; }\n"
        "    .toc-link:hover { text-decoration: underline; }\n"
        "    .main-content { margin-left: 220px; }\n"
        "    table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }\n"
        "    th, td { border: 1px solid rgb(221, 221, 221); padding: 8px 12px; text-align: left; }\n"
        "    th { background: rgb(245, 245, 245); font-weight: 600; }\n"
        "    tr:hover { background: rgb(250, 250, 250); }\n"
        "    .summary-row td { border-top: none; }\n"
        "    .summary-cell { padding-left: 2em; color: rgb(80, 80, 80); font-size: 0.9em; }\n"
        "    td.md p { margin: 0.25em 0; }\n"
        "    td.md p:first-child { margin-top: 0; }\n"
        "    td.md p:last-child { margin-bottom: 0; }\n"
        "    code { background: rgb(240, 240, 240); padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }\n"
        "    .artifacts-btn { cursor: pointer; padding: 2px 8px; font-size: 0.85em;"
        " border: 1px solid rgb(180, 180, 180); border-radius: 3px; background: rgb(250, 250, 250); }\n"
        "    .artifacts-btn:hover { background: rgb(235, 235, 235); }"
    )


def _find_test_artifact_runs(
    artifacts_root: Path,
    agent_name: AgentName,
    test_runs: tuple[TestRunInfo, ...],
) -> list[tuple[str, str, Path]]:
    """Find test artifact directories for all runs of an agent.

    Returns a list of (run_name, description, test_dir) tuples, one per run.
    Uses test_runs metadata when available to match run names to descriptions;
    otherwise discovers all run directories on disk.
    """
    agent_dir = artifacts_root / str(agent_name)
    if not agent_dir.is_dir():
        return []

    run_descriptions: dict[str, str] = {tr.run_name: tr.description_markdown for tr in test_runs}

    # Extracted layout from outputs.tar.gz: <agent_dir>/test_output/e2e/<run>/...
    test_output_dir = agent_dir / "test_output"
    found: list[tuple[str, str, Path]] = []
    for candidate_root in [test_output_dir / "e2e", test_output_dir, agent_dir / "e2e", agent_dir]:
        if not candidate_root.is_dir():
            continue
        for run_dir in sorted(candidate_root.iterdir()):
            if not run_dir.is_dir():
                continue
            for test_dir in sorted(run_dir.iterdir()):
                if test_dir.is_dir() and (test_dir / "transcript.txt").exists():
                    run_name = run_dir.name
                    description = run_descriptions.get(run_name, "")
                    found.append((run_name, description, test_dir))
    return found


def _build_artifact_panels(agent_artifact_runs: dict[str, list[tuple[str, str, Path]]]) -> str:
    """Build the overlay and slide-in panel divs for each agent's artifacts.

    When an agent has multiple runs, a tab bar is rendered at the top of the
    panel. Each tab shows the run number; clicking a tab switches the content.
    """
    if not agent_artifact_runs:
        return ""
    panels = '  <div id="artifacts-overlay" class="artifacts-overlay"></div>\n'
    for agent_name, runs in agent_artifact_runs.items():
        escaped_name = html.escape(agent_name)
        panels += (
            f'  <div class="artifacts-panel" id="panel-{escaped_name}">\n'
            f'    <button class="artifacts-close">&times;</button>\n'
            f"    <h2>{escaped_name}</h2>\n"
        )

        if len(runs) > 1:
            panels += '    <div class="run-tabs">\n'
            for i, (_run_name, _desc, _test_dir) in enumerate(runs):
                active = " active" if i == 0 else ""
                panels += (
                    f'      <button class="run-tab{active}" '
                    f'data-agent="{escaped_name}" data-run="{i}">{i + 1}</button>\n'
                )
            panels += "    </div>\n"

        for i, (_run_name, description, test_dir) in enumerate(runs):
            prefix = f"art-{escaped_name}-r{i}-"
            content = render_test_detail(test_dir, detail_id_prefix=prefix)
            display = "" if i == 0 else ' style="display:none"'
            panels += f'    <div class="run-content" data-agent="{escaped_name}" data-run="{i}"{display}>\n'
            if description:
                panels += f"      <p><em>{_render_markdown(description)}</em></p>\n"
            panels += f"      {content}\n"
            panels += "    </div>\n"

        panels += "  </div>\n"
    return panels


def _artifact_panel_css() -> str:
    """Return CSS for the artifact slide-in panels."""
    return (
        f"    {DETAIL_CSS}"
        "    .artifacts-overlay { display: none; position: fixed; inset: 0;"
        " background: rgba(0,0,0,0.3); z-index: 999; }\n"
        "    .artifacts-panel { display: none; position: fixed; top: 0; right: 0; bottom: 0;"
        " width: 80%; max-width: 1200px; background: rgb(250,250,250); z-index: 1000;"
        " overflow-y: auto; padding: 2rem; box-shadow: -4px 0 20px rgba(0,0,0,0.15); }\n"
        "    .artifacts-panel.open, .artifacts-overlay.open { display: block; }\n"
        "    .artifacts-close { position: sticky; top: 0; float: right; font-size: 1.5rem;"
        " cursor: pointer; background: none; border: none; padding: 0.5rem; z-index: 1001; }\n"
        "    .run-tabs { display: flex; gap: 4px; margin-bottom: 1rem; }\n"
        "    .run-tab { cursor: pointer; padding: 4px 12px; border: 1px solid rgb(200,200,200);"
        " border-radius: 4px; background: rgb(245,245,245); font-size: 0.85rem; }\n"
        "    .run-tab.active { background: rgb(33,150,243); color: white; border-color: rgb(33,150,243); }\n"
    )


def _artifact_panel_js() -> str:
    """Return JS for opening/closing artifact panels."""
    return """<script>
(function() {
  var overlay = document.getElementById('artifacts-overlay');
  function closePanel() {
    document.querySelectorAll('.artifacts-panel.open').forEach(function(p) { p.classList.remove('open'); });
    if (overlay) overlay.classList.remove('open');
  }
  document.querySelectorAll('.artifacts-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      closePanel();
      var agent = btn.getAttribute('data-agent');
      var panel = document.getElementById('panel-' + agent);
      if (panel) panel.classList.add('open');
      if (overlay) overlay.classList.add('open');
    });
  });
  if (overlay) overlay.addEventListener('click', closePanel);
  document.querySelectorAll('.artifacts-close').forEach(function(btn) {
    btn.addEventListener('click', closePanel);
  });
  document.querySelectorAll('.run-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var agent = tab.getAttribute('data-agent');
      var run = tab.getAttribute('data-run');
      tab.closest('.run-tabs').querySelectorAll('.run-tab').forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      tab.closest('.artifacts-panel').querySelectorAll('.run-content').forEach(function(c) {
        c.style.display = (c.getAttribute('data-run') === run) ? '' : 'none';
      });
    });
  });
})();
</script>"""
