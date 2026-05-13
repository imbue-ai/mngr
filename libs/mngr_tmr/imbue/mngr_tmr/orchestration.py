"""Polling and result-collection orchestration for the test-mapreduce plugin.

Drives the main map-reduce loop: poll each launched agent's volume for the
outputs archive, pull when it appears, and assemble per-test result records
for the report. The polling cadence is the existence check on the agent's
``outputs.tar.gz`` -- we never call ``mngr list``.
"""

import time
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TestMapReduceResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.launching import launch_agents_up_to_limit
from imbue.mngr_tmr.launching import stop_agent_on_host
from imbue.mngr_tmr.pulling import finalize_agent
from imbue.mngr_tmr.pulling import is_agent_outputs_ready
from imbue.mngr_tmr.pulling import is_integrator_outputs_ready
from imbue.mngr_tmr.report import generate_html_report


def launch_and_poll_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    max_agents: int,
    agent_timeout_seconds: float,
    poll_interval_seconds: float,
    report_path: Path | None,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    launch_failures: list[TestMapReduceResult],
    artifact_output_dir: Path | None = None,
    source_dir: Path | None = None,
) -> tuple[set[str], dict[str, TestResult]]:
    """Launch agents incrementally and poll until all finish.

    Handles two modes depending on arguments:

    1. Incremental launching (max_agents > 0, test_node_ids non-empty): launches
       up to max_agents at a time, polling and launching more as capacity opens.
    2. Pre-launched polling (test_node_ids empty, all_agents pre-populated):
       polls the already-launched agents without launching any new ones.

    all_agents, all_hosts, and launch_failures are input/output parameters:
    pre-existing entries are tracked from the start, and newly launched
    agents (or new launch failures) are appended during execution.

    Returns (timed_out_ids, cached_results).
    """
    remaining_tests = list(test_node_ids)
    pending_ids: set[str] = set()
    agent_id_to_info: dict[str, TestAgentInfo] = {}
    timed_out_ids: set[str] = set()
    cached_results: dict[str, TestResult] = {}

    for info in all_agents:
        pending_ids.add(str(info.agent_id))

    launch_kwargs: dict = {
        "remaining_tests": remaining_tests,
        "pending_ids": pending_ids,
        "max_agents": max_agents,
        "config": config,
        "mngr_ctx": mngr_ctx,
        "pytest_flags": pytest_flags,
        "prompt_suffix": prompt_suffix,
        "all_agents": all_agents,
        "all_hosts": all_hosts,
        "agent_id_to_info": agent_id_to_info,
        "launch_failures": launch_failures,
    }

    launch_agents_up_to_limit(**launch_kwargs)

    if report_path is not None:
        current_results = _build_current_results(all_agents, timed_out_ids, launch_failures)
        generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

    while pending_ids or remaining_tests:
        now = time.monotonic()
        changed = False

        for agent_id_str in list(pending_ids):
            info = agent_id_to_info[agent_id_str]
            host = all_hosts[agent_id_str]
            agent_id = AgentId(agent_id_str)

            if is_agent_outputs_ready(mngr_ctx, config.provider_name, host.id, agent_id):
                logger.info("Agent '{}' published outputs, finalizing", info.agent_name)
                pre_read = finalize_agent(
                    mngr_ctx=mngr_ctx,
                    provider_name=config.provider_name,
                    host=host,
                    agent_id=agent_id,
                    agent_name=info.agent_name,
                    branch_name=info.branch_name,
                    artifact_output_dir=artifact_output_dir,
                    source_dir=source_dir,
                    cg=mngr_ctx.concurrency_group,
                    should_stop=True,
                )
                if pre_read is not None:
                    cached_results[agent_id_str] = pre_read
                pending_ids.discard(agent_id_str)
                changed = True
                continue

            elapsed = now - info.created_at
            if elapsed >= agent_timeout_seconds:
                logger.warning(
                    "Agent '{}' timed out after {:.0f}s without publishing outputs, stopping",
                    info.agent_name,
                    elapsed,
                )
                stop_agent_on_host(host, agent_id, info.agent_name)
                pending_ids.discard(agent_id_str)
                timed_out_ids.add(agent_id_str)
                changed = True

        launch_agents_up_to_limit(**launch_kwargs)

        if changed and report_path is not None:
            current_results = _build_current_results(all_agents, timed_out_ids, launch_failures)
            generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

        if not pending_ids and not remaining_tests:
            break

        pending_names = [agent_id_to_info[aid].agent_name for aid in pending_ids]
        queued_msg = f", {len(remaining_tests)} queued" if remaining_tests else ""
        logger.info(
            "Polling {} pending agent(s){}: {}",
            len(pending_ids),
            queued_msg,
            ", ".join(str(n) for n in pending_names),
        )
        time.sleep(poll_interval_seconds)

    return timed_out_ids, cached_results


def _collect_agent_results(
    agents: list[TestAgentInfo],
    timed_out_ids: set[str],
    missing_summary: str,
    cached_results: dict[str, TestResult] | None = None,
) -> list[TestMapReduceResult]:
    """Shared iteration over agents to build result list.

    Relies entirely on ``cached_results``: an agent that finished writes its
    outcome into the outputs archive, which is downloaded and parsed during
    finalization. An agent with no cached result and not in ``timed_out_ids``
    has not produced outputs yet (intermediate report) or failed to do so
    before polling stopped (final report).
    """
    cached_results = cached_results or {}
    results: list[TestMapReduceResult] = []

    for agent_info in agents:
        agent_id_str = str(agent_info.agent_id)

        if agent_id_str in timed_out_ids:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    errored=True,
                    summary_markdown="Agent was stopped because the timeout was reached.",
                )
            )
            continue

        test_result = cached_results.get(agent_id_str)
        if test_result is not None:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    changes=test_result.changes,
                    errored=test_result.errored,
                    tests_passing_before=test_result.tests_passing_before,
                    tests_passing_after=test_result.tests_passing_after,
                    summary_markdown=test_result.summary_markdown,
                    branch_name=agent_info.branch_name,
                    test_runs=test_result.test_runs,
                )
            )
            continue

        results.append(
            TestMapReduceResult(
                test_node_id=agent_info.test_node_id,
                agent_name=agent_info.agent_name,
                errored=False,
                summary_markdown=missing_summary,
            )
        )

    return results


def gather_results(
    agents: list[TestAgentInfo],
    timed_out_ids: set[str],
    cached_results: dict[str, TestResult] | None = None,
    launch_failures: Sequence[TestMapReduceResult] = (),
) -> list[TestMapReduceResult]:
    """Gather results from all finished agents.

    Branches were already applied via each agent's bundle during the per-agent
    pull step, so this function just assembles the per-agent result records.
    ``launch_failures`` are prepended so agents that failed to launch still
    appear in the report.
    """
    results = _collect_agent_results(
        agents=agents,
        timed_out_ids=timed_out_ids,
        missing_summary="Agent never published outputs",
        cached_results=cached_results,
    )

    return [*launch_failures, *results]


def _build_current_results(
    agents: list[TestAgentInfo],
    timed_out_ids: set[str],
    launch_failures: Sequence[TestMapReduceResult] = (),
    cached_results: dict[str, TestResult] | None = None,
) -> list[TestMapReduceResult]:
    """Build current results without pulling branches, for intermediate reports.

    ``launch_failures`` are prepended so agents that failed to launch still
    appear in the report.
    """
    return [
        *launch_failures,
        *_collect_agent_results(
            agents=agents,
            timed_out_ids=timed_out_ids,
            missing_summary="Agent is still running...",
            cached_results=cached_results,
        ),
    ]


def wait_for_integrator(
    integrator: TestAgentInfo,
    poll_interval_seconds: float,
    host: OnlineHostInterface,
    deadline: float,
) -> str | None:
    """Poll for the integrator agent's outcome file and stop it once visible."""
    while time.monotonic() < deadline:
        if is_integrator_outputs_ready(integrator.work_dir, host):
            logger.info("Integrator outcome file detected, stopping agent")
            stop_agent_on_host(host, integrator.agent_id, integrator.agent_name)
            return integrator.branch_name
        time.sleep(poll_interval_seconds)

    logger.warning("Integrator agent timed out, stopping it")
    stop_agent_on_host(host, integrator.agent_id, integrator.agent_name)
    return None
