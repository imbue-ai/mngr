"""HTML report generation for the test-mapreduce plugin.

The reporter takes a list of ``AgentMetadata`` from orchestration and
reads each agent's outcome JSON from ``output_dir/<agent_name>/`` on
demand. Outcome JSON shape is a contract between the agents and this
module; orchestration does not parse it. Parsed outcomes are cached
in-process (test-agent outcomes and the integrator outcome are
immutable once an agent has published them, so caching is safe).

The HTML template, the CSS, and the panel JS live under ``report_assets/``
and are rendered with Jinja2. This module's job is to assemble the
context dict the template renders against.

The test-mapreduce-specific data types (``TestResult``, ``Change``, etc.)
also live here -- they're only used by this module. Framework-side types
live in ``imbue.mngr_mapreduce.data_types``.
"""

import html
import json
from collections.abc import Sequence
from enum import auto
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment
from jinja2 import PackageLoader
from jinja2 import select_autoescape
from loguru import logger
from markdown_it import MarkdownIt
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.detail_renderer import ASCIINEMA_PLAYER_CSS
from imbue.mngr.utils.detail_renderer import ASCIINEMA_PLAYER_JS
from imbue.mngr.utils.detail_renderer import DETAIL_CSS
from imbue.mngr.utils.detail_renderer import render_test_detail
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_tmr.prompts import INTEGRATOR_OUTCOME_FILENAME
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME


class ChangeKind(UpperCaseStrEnum):
    """What kind of change the agent attempted."""

    IMPROVE_TEST = auto()
    FIX_TEST = auto()
    FIX_IMPL = auto()
    FIX_TUTORIAL = auto()


class ChangeStatus(UpperCaseStrEnum):
    """Whether the change succeeded."""

    SUCCEEDED = auto()
    FAILED = auto()
    BLOCKED = auto()


class Change(FrozenModel):
    """One change the agent attempted."""

    status: ChangeStatus = Field(description="Whether the change succeeded, failed, or is blocked")
    summary_markdown: str = Field(description="Markdown description of what was done or attempted")


class ReportSection(UpperCaseStrEnum):
    """Derived section for HTML report grouping and coloring.

    BLOCKED is reserved for results where the coding agent itself decided
    the work was too complex (i.e. produced changes whose status is BLOCKED).
    FAILED is reserved for infrastructure failures: launch failures, agent
    timeouts, missing details, etc. -- cases where the agent never had a
    chance to produce a real verdict.
    """

    NON_IMPL_FIXES = auto()
    IMPL_FIXES = auto()
    BLOCKED = auto()
    FAILED = auto()
    CLEAN_PASS = auto()
    RUNNING = auto()


class TestRunInfo(FrozenModel):
    """Metadata for a single test run within an agent's work."""

    # Tell pytest not to collect this as a test class (its name starts with "Test").
    __test__ = False

    run_name: str = Field(description="The --mngr-e2e-run-name value used for this run")
    description_markdown: str = Field(description="Brief description of what this run was for")


class TestResult(FrozenModel):
    """Result reported by a test agent, read from its outcome JSON."""

    # Tell pytest not to collect this as a test class (its name starts with "Test").
    __test__ = False

    changes: dict[ChangeKind, Change] = Field(
        default_factory=dict, description="Changes the agent attempted, keyed by kind"
    )
    errored: bool = Field(
        default=False, description="Whether an infrastructure error prevented the agent from working"
    )
    tests_passing_before: bool | None = Field(
        default=None, description="Were tests passing before any changes? None if unknown."
    )
    tests_passing_after: bool | None = Field(
        default=None, description="Are tests passing after all changes? None if unknown."
    )
    summary_markdown: str = Field(default="", description="Overall markdown summary of what happened")
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="List of test runs performed, in order")


class Normalization(FrozenModel):
    """A suite-wide cleanup the integrator applied during the normalize stage."""

    summary_markdown: str = Field(description="Markdown description of the cleanup that was applied and verified")


class Escalation(FrozenModel):
    """A cross-cutting blocker the integrator could not resolve, surfaced to the user."""

    title: str = Field(description="Short title of the blocker")
    detail_markdown: str = Field(description="Markdown detail of what is needed to resolve it")


class IntegratorResult(FrozenModel):
    """Result from the integrator agent that cherry-picks fix branches."""

    agent_name: AgentName | None = Field(default=None, description="Name of the integrator agent")
    squashed_branches: tuple[str, ...] = Field(default=(), description="Branches in the squashed non-impl commit")
    squashed_commit_hash: str | None = Field(default=None, description="Commit hash of the squashed non-impl commit")
    impl_priority: tuple[str, ...] = Field(default=(), description="Impl branches in priority order, highest first")
    impl_commit_hashes: dict[str, str] = Field(
        default_factory=dict, description="Mapping of impl branch name to its commit hash on the integrated branch"
    )
    failed: tuple[str, ...] = Field(default=(), description="Branch names that could not be integrated")
    branch_name: str | None = Field(default=None, description="Integrated branch name, if any merges succeeded")
    normalizations: tuple[Normalization, ...] = Field(
        default=(), description="Suite-wide cleanups applied and verified during the normalize stage"
    )
    escalations: tuple[Escalation, ...] = Field(
        default=(), description="Cross-cutting blockers the integrator could not resolve, surfaced to the user"
    )


class TestMapReduceResult(FrozenModel):
    """Result for one test in the map-reduce run."""

    # Tell pytest not to collect this as a test class (its name starts with "Test").
    __test__ = False

    test_node_id: str = Field(description="The pytest node ID for the test")
    agent_name: AgentName = Field(description="Name of the agent that ran this test")
    changes: dict[ChangeKind, Change] = Field(
        default_factory=dict, description="Changes the agent attempted, keyed by kind"
    )
    errored: bool = Field(default=False, description="Whether an error prevented the agent from working")
    tests_passing_before: bool | None = Field(default=None, description="Were tests passing before changes?")
    tests_passing_after: bool | None = Field(default=None, description="Are tests passing after changes?")
    summary_markdown: str = Field(default="", description="Markdown summary from the agent")
    branch_name: str | None = Field(
        default=None,
        description="Git branch name if code changes were pulled, or None",
    )
    test_runs: tuple[TestRunInfo, ...] = Field(default=(), description="Test runs performed by the agent, in order")


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

_CHANGE_STATUS_ICONS: dict[ChangeStatus, str] = {
    ChangeStatus.SUCCEEDED: "&#10003;",
    ChangeStatus.FAILED: "&#10007;",
    ChangeStatus.BLOCKED: "&#9644;",
}


# The Jinja env autoescapes the .j2 template; sections that already contain
# safe HTML (markdown-rendered cells, test ids with <wbr> hints) are passed
# through with the |safe filter in the template.
_jinja_env = Environment(
    loader=PackageLoader("imbue.mngr_tmr", "report_assets"),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


def _read_static(filename: str) -> str:
    """Read a static (non-jinja) asset shipped under report_assets/."""
    return (files("imbue.mngr_tmr.report_assets") / filename).read_text()


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
        normalizations=tuple(
            Normalization(summary_markdown=entry.get("summary_markdown", ""))
            for entry in data.get("normalizations", ())
        ),
        escalations=tuple(
            Escalation(title=entry.get("title", ""), detail_markdown=entry.get("detail_markdown", ""))
            for entry in data.get("escalations", ())
        ),
    )
    _INTEGRATOR_OUTCOME_CACHE[meta.agent_name] = result
    return result


def _row_from_metadata(meta: AgentMetadata, outcome: TestResult | None) -> TestMapReduceResult:
    """Build a renderable row from per-agent metadata + optional parsed outcome."""
    if meta.error_summary is not None:
        return TestMapReduceResult(
            test_node_id=meta.task_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            errored=True,
            summary_markdown=meta.error_summary,
            branch_name=meta.branch_name,
        )
    if outcome is None:
        return TestMapReduceResult(
            test_node_id=meta.task_id or str(meta.agent_name),
            agent_name=meta.agent_name,
            summary_markdown="Agent is still running...",
            branch_name=meta.branch_name,
        )
    return TestMapReduceResult(
        test_node_id=meta.task_id or str(meta.agent_name),
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
        if meta.kind is not AgentKind.MAPPER:
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


def _format_test_id(test_node_id: str) -> str:
    """HTML-escape the node ID, then add a soft wrap hint after each ``::``."""
    return html.escape(test_node_id).replace("::", "::<wbr>")


def _format_changes(changes: dict[ChangeKind, Change]) -> str:
    """Format changes as concise kind + icon pairs."""
    parts: list[str] = []
    for kind, change in changes.items():
        icon = _CHANGE_STATUS_ICONS.get(change.status, "?")
        parts.append(f"{kind.value} {icon}")
    return ", ".join(parts)


def _merged_status_html(result: TestMapReduceResult, integrator: IntegratorResult | None) -> str:
    """Return merged-status HTML: commit hash for impl, checkmark for squashed, X for failed."""
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


def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return _md.render(text)


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


def _build_row_view(
    row: TestMapReduceResult,
    integrator: IntegratorResult | None,
    has_artifacts_for_agent: bool,
) -> dict[str, object]:
    """Flatten a renderable row into the dict the jinja template consumes."""
    return {
        "test_id_html": _format_test_id(row.test_node_id),
        "agent_name": str(row.agent_name),
        "branch_name": row.branch_name,
        "changes_html": _format_changes(row.changes) if row.changes else "-",
        "merged_html": _merged_status_html(row, integrator),
        "summary_html": _render_markdown(row.summary_markdown) if row.summary_markdown else "",
        "has_artifacts": has_artifacts_for_agent,
    }


def _build_section_views(
    rows: list[TestMapReduceResult],
    integrator: IntegratorResult | None,
    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]],
    has_artifacts: bool,
) -> list[dict[str, object]]:
    """Group rows by section and prepare the section views the template consumes."""
    grouped: dict[ReportSection, list[TestMapReduceResult]] = {}
    for r in rows:
        grouped.setdefault(_report_section_of(r), []).append(r)

    sections: list[dict[str, object]] = []
    for sec in _SECTION_ORDER:
        group = grouped.get(sec)
        if not group:
            continue
        if sec == ReportSection.IMPL_FIXES and integrator is not None and integrator.impl_priority:
            priority_order = {branch: i for i, branch in enumerate(integrator.impl_priority)}
            group = sorted(group, key=lambda r: priority_order.get(r.branch_name or "", len(priority_order)))
        col_count_base = (
            5
            if sec not in (ReportSection.RUNNING, ReportSection.CLEAN_PASS)
            else (2 if sec == ReportSection.RUNNING else 3)
        )
        col_count = col_count_base + (1 if has_artifacts and sec != ReportSection.RUNNING else 0)
        section_rows = [_build_row_view(r, integrator, str(r.agent_name) in agent_artifact_runs) for r in group]
        sections.append(
            {
                "kind": sec.value,
                "label": _SECTION_LABELS[sec],
                "color": _SECTION_COLORS[sec],
                "anchor": f"sec-{sec.value}",
                "rows": section_rows,
                "count": len(section_rows),
                "col_count": col_count,
            }
        )
    return sections


def _build_toc_links(sections: list[dict[str, object]]) -> list[dict[str, object]]:
    """One sidebar entry per non-empty section."""
    return [
        {
            "anchor": s["anchor"],
            "color": s["color"],
            "label": s["label"],
            "count": s["count"],
        }
        for s in sections
    ]


def _build_artifact_panels(
    agent_artifact_runs: dict[str, list[tuple[str, str, Path]]],
) -> list[dict[str, object]]:
    """Build the panel views, one per agent that has artifact runs."""
    panels: list[dict[str, object]] = []
    for agent_name, runs in agent_artifact_runs.items():
        escaped_name = html.escape(agent_name)
        run_views: list[dict[str, object]] = []
        for i, (_run_name, description, test_dir) in enumerate(runs):
            prefix = f"art-{escaped_name}-r{i}-"
            run_views.append(
                {
                    "index": i,
                    "description_html": _render_markdown(description) if description else "",
                    "detail_html": render_test_detail(test_dir, detail_id_prefix=prefix),
                }
            )
        panels.append(
            {
                "agent_name": agent_name,
                "tab_count": len(runs),
                "runs": run_views,
            }
        )
    return panels


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
    report to s3 is the recipe's responsibility (see ``recipe.render_report``).
    """
    rows = _build_rows(agents, output_dir)
    integrator = _load_integrator_outcome(integrator_metadata, output_dir) if integrator_metadata is not None else None

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

    has_artifacts = bool(agent_artifact_runs)
    sections = _build_section_views(rows, integrator, agent_artifact_runs, has_artifacts)
    toc_links = _build_toc_links(sections)
    artifact_panels = _build_artifact_panels(agent_artifact_runs)

    # Title is autoescaped by the template; detail/summary are markdown rendered
    # to HTML here and passed through with |safe, like the per-row summary cells.
    escalation_views = (
        [{"title": e.title, "detail_html": _render_markdown(e.detail_markdown)} for e in integrator.escalations]
        if integrator is not None
        else []
    )
    normalization_views = (
        [{"summary_html": _render_markdown(n.summary_markdown)} for n in integrator.normalizations]
        if integrator is not None
        else []
    )

    reintegrate_cmd = ""
    if run_commands:
        for cmd_label, cmd_text in run_commands:
            if "reintegrate" in cmd_label.lower():
                reintegrate_cmd = html.escape(cmd_text)
                break

    template = _jinja_env.get_template("report.html.j2")
    report_html = template.render(
        rows=rows,
        sections=sections,
        toc_links=toc_links,
        has_artifacts=has_artifacts,
        artifact_panels=artifact_panels,
        integrator=integrator,
        escalation_views=escalation_views,
        normalization_views=normalization_views,
        run_commands=run_commands or [],
        reintegrate_cmd=reintegrate_cmd,
        asciinema_css_url=ASCIINEMA_PLAYER_CSS,
        asciinema_js_url=ASCIINEMA_PLAYER_JS,
        css=_read_static("report.css"),
        detail_css=DETAIL_CSS,
        js=_read_static("artifacts.js"),
    )
    output_path = output_dir / "index.html"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("HTML report written to {}", output_path)
    return output_path
