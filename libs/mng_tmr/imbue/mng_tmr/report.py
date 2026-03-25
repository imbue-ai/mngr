"""HTML report generation for the test-mapreduce plugin.

Builds a self-contained HTML page with category navigation bars, per-category
tables, an optional integrator section, and embedded test artifacts.
"""

import html
from pathlib import Path

from loguru import logger
from markdown_it import MarkdownIt

from imbue.mng.e2e.test_detail_renderer import ASCIINEMA_PLAYER_CSS
from imbue.mng.e2e.test_detail_renderer import ASCIINEMA_PLAYER_JS
from imbue.mng.e2e.test_detail_renderer import DETAIL_CSS
from imbue.mng.e2e.test_detail_renderer import render_test_detail
from imbue.mng.primitives import AgentName
from imbue.mng_tmr.data_types import ChangeStatus
from imbue.mng_tmr.data_types import DisplayCategory
from imbue.mng_tmr.data_types import IntegratorResult
from imbue.mng_tmr.data_types import TestMapReduceResult

_DISPLAY_COLORS: dict[DisplayCategory, str] = {
    DisplayCategory.PENDING: "rgb(3, 169, 244)",
    DisplayCategory.FIXED: "rgb(33, 150, 243)",
    DisplayCategory.REGRESSED: "rgb(255, 152, 0)",
    DisplayCategory.STUCK: "rgb(244, 67, 54)",
    DisplayCategory.ERRORED: "rgb(158, 158, 158)",
    DisplayCategory.CLEAN_PASS: "rgb(76, 175, 80)",
}

_DISPLAY_GROUP_ORDER: list[DisplayCategory] = [
    DisplayCategory.PENDING,
    DisplayCategory.FIXED,
    DisplayCategory.REGRESSED,
    DisplayCategory.STUCK,
    DisplayCategory.ERRORED,
    DisplayCategory.CLEAN_PASS,
]

_md = MarkdownIt()


def display_category_of(result: TestMapReduceResult) -> DisplayCategory:
    """Derive a display category from a result for report grouping/coloring."""
    if result.errored:
        return DisplayCategory.ERRORED
    if result.tests_passing_before is None and result.tests_passing_after is None and not result.changes:
        return DisplayCategory.PENDING
    has_succeeded = any(c.status == ChangeStatus.SUCCEEDED for c in result.changes.values())
    if has_succeeded:
        if result.tests_passing_before is True and result.tests_passing_after is not True:
            return DisplayCategory.REGRESSED
        return DisplayCategory.FIXED
    if not result.changes and result.tests_passing_after is True:
        return DisplayCategory.CLEAN_PASS
    return DisplayCategory.STUCK


def generate_html_report(
    results: list[TestMapReduceResult],
    output_path: Path,
    integrator: IntegratorResult | None = None,
    test_artifacts_dir: Path | None = None,
) -> Path:
    """Generate an HTML report summarizing test-mapreduce results."""
    counts: dict[DisplayCategory, int] = {}
    for r in results:
        cat = display_category_of(r)
        counts[cat] = counts.get(cat, 0) + 1

    # Find artifact directories per agent
    artifact_dirs: dict[str, Path] = {}
    if test_artifacts_dir is not None:
        for r in results:
            artifact_dir = _find_test_artifact_dir(test_artifacts_dir, r.agent_name)
            if artifact_dir is not None:
                artifact_dirs[str(r.agent_name)] = artifact_dir

    nav_html = _build_category_nav(counts, len(results))
    tables_html = _build_grouped_tables(results, artifact_dirs)
    integrator_html = _build_integrator_section(integrator)
    integrator_nav = _build_integrator_nav(integrator)
    panels_html = _build_artifact_panels(artifact_dirs)

    has_artifacts = bool(artifact_dirs)
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
  <h1>Test Map-Reduce Report</h1>
  <p class="summary">{len(results)} test(s)</p>
{nav_html}
{integrator_nav}
{tables_html}
{integrator_html}
{panels_html}
{artifact_js}
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_html)
    logger.info("HTML report written to {}", output_path)
    return output_path


def _build_category_nav(counts: dict[DisplayCategory, int], total: int) -> str:
    """Build a list of horizontal bars, one per category, each linking to its section."""
    if total == 0:
        return ""
    max_count = max(counts.values()) if counts else 1
    rows = ""
    for cat in _DISPLAY_GROUP_ORDER:
        count = counts.get(cat, 0)
        if count == 0:
            continue
        pct = count / max_count * 100
        color = _DISPLAY_COLORS.get(cat, "rgb(158, 158, 158)")
        anchor = f"cat-{cat.value}"
        rows += (
            f'  <a href="#{anchor}" class="nav-row">'
            f'<span class="nav-label">{cat.value} ({count})</span>'
            f'<span class="nav-bar" style="width: {pct:.1f}%; background: {color};"></span>'
            f"</a>\n"
        )
    return f'  <div class="nav">\n{rows}  </div>'


def _build_integrator_nav(integrator: IntegratorResult | None) -> str:
    """Build an anchor link to the integrator section, showing agent name and branch."""
    if integrator is None:
        return ""
    details: list[str] = []
    if integrator.agent_name is not None:
        details.append(f"<code>{html.escape(str(integrator.agent_name))}</code>")
    if integrator.branch_name is not None:
        details.append(f"<code>{html.escape(integrator.branch_name)}</code>")
    suffix = f" -- {' '.join(details)}" if details else ""
    return f'  <p class="integrator-nav"><a href="#integrator">Integrator{suffix}</a></p>\n'


def _build_integrator_section(integrator: IntegratorResult | None) -> str:
    """Build the HTML section for the integrator agent results."""
    if integrator is None:
        return ""
    section = '  <h2 id="integrator" class="integrator-header">Integrator</h2>\n'
    if integrator.agent_name is not None:
        escaped_name = html.escape(str(integrator.agent_name))
        section += f"  <p>Agent: <code>{escaped_name}</code></p>\n"
    if integrator.branch_name is not None:
        escaped = html.escape(integrator.branch_name)
        section += f'  <p class="integrator">Integrated branch: <code>{escaped}</code></p>\n'
    if integrator.merged:
        section += "  <p>Merged:</p>\n  <ul>\n"
        for b in integrator.merged:
            section += f"    <li><code>{html.escape(b)}</code></li>\n"
        section += "  </ul>\n"
    if integrator.failed:
        section += '  <p style="color: rgb(244, 67, 54);">Failed to merge:</p>\n  <ul>\n'
        for b in integrator.failed:
            section += f"    <li><code>{html.escape(b)}</code></li>\n"
        section += "  </ul>\n"
    if integrator.summary_markdown:
        section += f'  <div class="md">{_render_markdown(integrator.summary_markdown)}</div>\n'
    return section


def _render_markdown(text: str) -> str:
    """Render markdown text to HTML."""
    return _md.render(text)


def _build_grouped_tables(
    results: list[TestMapReduceResult],
    artifact_dirs: dict[str, Path] | None = None,
) -> str:
    """Build HTML tables grouped by display category, with CLEAN_PASS last."""
    artifact_dirs = artifact_dirs or {}
    grouped: dict[DisplayCategory, list[TestMapReduceResult]] = {}
    for r in results:
        cat = display_category_of(r)
        grouped.setdefault(cat, []).append(r)

    sections = ""
    for cat in _DISPLAY_GROUP_ORDER:
        group = grouped.get(cat)
        if not group:
            continue
        color = _DISPLAY_COLORS.get(cat, "rgb(158, 158, 158)")
        anchor = f"cat-{cat.value}"
        sections += f'  <h2 id="{anchor}" style="color: {color};">{cat.value} ({len(group)})</h2>\n'
        sections += "  <table>\n    <thead>\n      <tr>"
        sections += "<th>Test</th><th>Changes</th><th>Summary</th><th>Agent</th><th>Branch</th>"
        if artifact_dirs:
            sections += "<th>Artifacts</th>"
        sections += "</tr>\n    </thead>\n    <tbody>\n"
        for r in group:
            branch_cell = r.branch_name if r.branch_name else "-"
            summary_html = _render_markdown(r.summary_markdown)
            changes_cell = (
                ", ".join(f"{kind.value}/{change.status.value}" for kind, change in r.changes.items())
                if r.changes
                else "-"
            )
            agent_name_str = str(r.agent_name)
            artifact_cell = ""
            if artifact_dirs:
                if agent_name_str in artifact_dirs:
                    escaped = html.escape(agent_name_str)
                    artifact_cell = f'<td><button class="artifacts-btn" data-agent="{escaped}">View</button></td>'
                else:
                    artifact_cell = "<td>-</td>"
            sections += f"""      <tr>
        <td>{html.escape(r.test_node_id)}</td>
        <td>{html.escape(changes_cell)}</td>
        <td class="md">{summary_html}</td>
        <td><code>{html.escape(agent_name_str)}</code></td>
        <td><code>{html.escape(branch_cell)}</code></td>
        {artifact_cell}
      </tr>
"""
        sections += "    </tbody>\n  </table>\n"

    return sections


def _html_report_css() -> str:
    """Return the CSS stylesheet for the HTML report.

    Uses rgb() colors instead of hex to avoid ratchet false positives.
    """
    return (
        "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; }\n"
        "    h1 { color: rgb(51, 51, 51); }\n"
        "    h2 { margin-top: 1.5rem; font-size: 1.1rem; }\n"
        "    .summary { margin-bottom: 0.5rem; color: rgb(102, 102, 102); }\n"
        "    .nav { margin-bottom: 1.5rem; }\n"
        "    .nav-row { display: flex; align-items: center; text-decoration: none;"
        " margin-bottom: 4px; gap: 8px; }\n"
        "    .nav-row:hover { opacity: 0.8; }\n"
        "    .nav-label { font-weight: 600; font-size: 0.9rem; min-width: 180px;"
        " color: rgb(51, 51, 51); }\n"
        "    .nav-bar { height: 20px; border-radius: 3px; min-width: 4px; }\n"
        "    .integrator-nav { margin-bottom: 1rem; }\n"
        "    .integrator-nav a { color: rgb(33, 150, 243); font-weight: 600;"
        " text-decoration: none; }\n"
        "    .integrator-nav a:hover { text-decoration: underline; }\n"
        "    table { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }\n"
        "    th, td { border: 1px solid rgb(221, 221, 221); padding: 8px 12px; text-align: left; }\n"
        "    th { background: rgb(245, 245, 245); font-weight: 600; }\n"
        "    tr:hover { background: rgb(250, 250, 250); }\n"
        "    td.md p { margin: 0.25em 0; }\n"
        "    td.md p:first-child { margin-top: 0; }\n"
        "    td.md p:last-child { margin-bottom: 0; }\n"
        "    code { background: rgb(240, 240, 240); padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }\n"
        "    .integrator { color: rgb(33, 150, 243); font-weight: 600; }\n"
        "    .artifacts-btn { cursor: pointer; padding: 2px 8px; font-size: 0.85em;"
        " border: 1px solid rgb(180, 180, 180); border-radius: 3px; background: rgb(250, 250, 250); }\n"
        "    .artifacts-btn:hover { background: rgb(235, 235, 235); }"
    )


def _find_test_artifact_dir(artifacts_root: Path, agent_name: AgentName) -> Path | None:
    """Find the test artifact directory for an agent.

    The structure after pull_test_outputs is:
      <artifacts_root>/<agent_name>/e2e/<run_name>/<test_name>/
    Since each TMR agent runs one test, there should be one run and one test dir.
    """
    agent_dir = artifacts_root / str(agent_name)
    if not agent_dir.is_dir():
        return None
    # Walk: agent_dir / e2e / <run> / <test> -- or agent_dir / <run> / <test> if no e2e subdir
    for candidate_root in [agent_dir / "e2e", agent_dir]:
        if not candidate_root.is_dir():
            continue
        for run_dir in sorted(candidate_root.iterdir()):
            if not run_dir.is_dir():
                continue
            for test_dir in sorted(run_dir.iterdir()):
                if test_dir.is_dir() and (test_dir / "transcript.txt").exists():
                    return test_dir
    return None


def _build_artifact_panels(artifact_dirs: dict[str, Path]) -> str:
    """Build the overlay and slide-in panel divs for each agent's artifacts."""
    if not artifact_dirs:
        return ""
    panels = '  <div id="artifacts-overlay" class="artifacts-overlay"></div>\n'
    for agent_name, test_dir in artifact_dirs.items():
        escaped_name = html.escape(agent_name)
        prefix = f"art-{escaped_name}-"
        content = render_test_detail(test_dir, detail_id_prefix=prefix)
        panels += (
            f'  <div class="artifacts-panel" id="panel-{escaped_name}">\n'
            f'    <button class="artifacts-close">&times;</button>\n'
            f"    <h2>{escaped_name}</h2>\n"
            f"    {content}\n"
            f"  </div>\n"
        )
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
})();
</script>"""
