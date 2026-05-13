"""Polling and result-collection orchestration for the test-mapreduce plugin.

Drives the main map-reduce loop: poll launched agents, finalize them when
they finish, and assemble per-test result records for the report.
"""

import time
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TestMapReduceResult
from imbue.mngr_tmr.data_types import TestResult
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.launching import launch_agents_up_to_limit
from imbue.mngr_tmr.launching import stop_agent_on_host
from imbue.mngr_tmr.mngr_cli import try_list_agents
from imbue.mngr_tmr.pulling import finalize_agent
from imbue.mngr_tmr.pulling import is_agent_outputs_ready
from imbue.mngr_tmr.pulling import try_read_integrator_outcome
from imbue.mngr_tmr.report import generate_html_report

_TERMINAL_STATES = frozenset(
    {
        AgentLifecycleState.DONE,
        AgentLifecycleState.STOPPED,
        AgentLifecycleState.WAITING,
    }
)

_MISSING_AGENT_MAX_ROUNDS = 30

# How many poll cycles we wait for the outputs archive to become visible
# on the agent's volume after the agent reaches a terminal lifecycle state.
# Volumes (notably Modal) propagate writes asynchronously, so we give the
# orchestrator's view a chance to catch up before declaring the agent
# failed. At the default 10s poll cadence this is ~2 minutes.
_TERMINAL_WITHOUT_OUTPUTS_MAX_ROUNDS = 12


def launch_and_poll_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    max_agents: int,
    agent_timeout_seconds: float,
    poll_interval_seconds: float,
    result_check_interval_seconds: float,
    report_path: Path | None,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    launch_failures: list[TestMapReduceResult],
    artifact_output_dir: Path | None = None,
    source_dir: Path | None = None,
) -> tuple[dict[str, AgentDetails], set[str], dict[str, TestResult]]:
    """Launch agents incrementally and poll until all finish.

    Handles two modes depending on arguments:

    1. Incremental launching (max_agents > 0, test_node_ids non-empty): launches
       up to max_agents at a time, polling and launching more as capacity opens.
    2. Pre-launched polling (test_node_ids empty, all_agents pre-populated):
       polls the already-launched agents without launching any new ones.

    all_agents, all_hosts, and launch_failures are input/output parameters:
    pre-existing entries are tracked from the start, and newly launched
    agents (or new launch failures) are appended during execution.

    Returns (final_details, timed_out_ids, cached_results).
    """
    remaining_tests = list(test_node_ids)
    pending_ids: set[str] = set()
    agent_id_to_info: dict[str, TestAgentInfo] = {}
    final_details: dict[str, AgentDetails] = {}
    timed_out_ids: set[str] = set()
    missing_rounds: dict[str, int] = {}
    cached_results: dict[str, TestResult] = {}
    last_result_check: dict[str, float] = {}
    terminal_without_outputs_rounds: dict[str, int] = {}

    for info in all_agents:
        agent_id_str = str(info.agent_id)
        agent_id_to_info[agent_id_str] = info
        pending_ids.add(agent_id_str)
        last_result_check[agent_id_str] = info.created_at

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
    for aid in pending_ids:
        if aid not in last_result_check:
            last_result_check[aid] = agent_id_to_info[aid].created_at

    if report_path is not None:
        current_results = _build_current_results(all_agents, final_details, timed_out_ids, launch_failures)
        generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

    while pending_ids or remaining_tests:
        now = time.monotonic()
        timed_out_this_round = False
        for agent_id_str in list(pending_ids):
            info = agent_id_to_info[agent_id_str]
            elapsed = now - info.created_at
            if elapsed >= agent_timeout_seconds:
                host = all_hosts[agent_id_str]
                if is_agent_outputs_ready(mngr_ctx, config.provider_name, host.id, AgentId(agent_id_str)):
                    logger.info(
                        "Agent '{}' has outputs archive (found before timeout stop), treating as done",
                        info.agent_name,
                    )
                    pending_ids.discard(agent_id_str)
                    continue
                logger.warning("Agent '{}' timed out after {:.0f}s, stopping", info.agent_name, elapsed)
                stop_agent_on_host(host, AgentId(agent_id_str), info.agent_name)
                pending_ids.discard(agent_id_str)
                timed_out_ids.add(agent_id_str)
                timed_out_this_round = True

        launch_agents_up_to_limit(**launch_kwargs)
        for aid in pending_ids:
            if aid not in last_result_check:
                last_result_check[aid] = agent_id_to_info[aid].created_at

        if timed_out_this_round and report_path is not None:
            current_results = _build_current_results(all_agents, final_details, timed_out_ids, launch_failures)
            generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

        if not pending_ids and not remaining_tests:
            break

        if not pending_ids:
            continue

        pending_names = [agent_id_to_info[aid].agent_name for aid in pending_ids]
        queued_msg = f", {len(remaining_tests)} queued" if remaining_tests else ""
        logger.info(
            "Polling {} pending agent(s){}: {}",
            len(pending_ids),
            queued_msg,
            ", ".join(str(n) for n in pending_names),
        )
        list_result = try_list_agents(mngr_ctx)
        if list_result is None:
            time.sleep(poll_interval_seconds)
            continue

        seen_ids: set[str] = set()
        changed = False
        for agent_detail in list_result.agents:
            agent_id_str = str(agent_detail.id)
            seen_ids.add(agent_id_str)
            if agent_id_str not in pending_ids:
                continue
            if agent_detail.state not in _TERMINAL_STATES:
                continue

            host = all_hosts[agent_id_str]
            archive_ready = is_agent_outputs_ready(mngr_ctx, config.provider_name, host.id, agent_detail.id)
            if not archive_ready:
                # The agent's lifecycle says it's done but the outputs archive
                # is not visible on the volume yet -- typically a volume
                # propagation delay. Keep it pending and try again next cycle.
                # Give up after a grace window so a genuinely-crashed agent
                # doesn't keep the run from finishing.
                rounds = terminal_without_outputs_rounds.get(agent_id_str, 0) + 1
                terminal_without_outputs_rounds[agent_id_str] = rounds
                if rounds < _TERMINAL_WITHOUT_OUTPUTS_MAX_ROUNDS:
                    logger.info(
                        "Agent '{}' in state {} but outputs not yet visible (round {}/{})",
                        agent_detail.name,
                        agent_detail.state,
                        rounds,
                        _TERMINAL_WITHOUT_OUTPUTS_MAX_ROUNDS,
                    )
                    continue
                logger.warning(
                    "Agent '{}' has been in state {} for {} rounds without publishing outputs; giving up",
                    agent_detail.name,
                    agent_detail.state,
                    rounds,
                )

            logger.info("Agent '{}' finished (state={})", agent_detail.name, agent_detail.state)
            final_details[agent_id_str] = agent_detail
            pending_ids.discard(agent_id_str)
            missing_rounds.pop(agent_id_str, None)
            terminal_without_outputs_rounds.pop(agent_id_str, None)
            changed = True

            pre_read = finalize_agent(
                mngr_ctx=mngr_ctx,
                provider_name=config.provider_name,
                host=host,
                agent_id=agent_detail.id,
                agent_name=agent_detail.name,
                branch_name=agent_detail.initial_branch,
                artifact_output_dir=artifact_output_dir,
                source_dir=source_dir,
                cg=mngr_ctx.concurrency_group,
                should_stop=agent_detail.state == AgentLifecycleState.WAITING,
            )
            if pre_read is not None:
                cached_results[agent_id_str] = pre_read

        for agent_id_str in list(pending_ids):
            if agent_id_str not in seen_ids:
                rounds = missing_rounds.get(agent_id_str, 0) + 1
                missing_rounds[agent_id_str] = rounds
                if rounds >= _MISSING_AGENT_MAX_ROUNDS:
                    logger.warning("Agent {} disappeared after {} rounds, treating as error", agent_id_str, rounds)
                    pending_ids.discard(agent_id_str)
                    changed = True

        launch_agents_up_to_limit(**launch_kwargs)
        for aid in pending_ids:
            if aid not in last_result_check:
                last_result_check[aid] = agent_id_to_info[aid].created_at

        for agent_id_str in list(pending_ids):
            if now - last_result_check[agent_id_str] >= result_check_interval_seconds:
                last_result_check[agent_id_str] = now
                info = agent_id_to_info[agent_id_str]
                host = all_hosts[agent_id_str]
                if is_agent_outputs_ready(mngr_ctx, config.provider_name, host.id, AgentId(agent_id_str)):
                    logger.info(
                        "Agent '{}' has outputs archive (detected via direct check), treating as done",
                        info.agent_name,
                    )
                    pre_read = finalize_agent(
                        mngr_ctx=mngr_ctx,
                        provider_name=config.provider_name,
                        host=host,
                        agent_id=AgentId(agent_id_str),
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

        if (changed or timed_out_this_round) and report_path is not None:
            current_results = _build_current_results(all_agents, final_details, timed_out_ids, launch_failures)
            generate_html_report(current_results, report_path, test_artifacts_dir=artifact_output_dir)

        if pending_ids or remaining_tests:
            time.sleep(poll_interval_seconds)

    return final_details, timed_out_ids, cached_results


def _collect_agent_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    missing_detail_errored: bool,
    missing_detail_summary: str,
    cached_results: dict[str, TestResult] | None = None,
) -> list[TestMapReduceResult]:
    """Shared iteration over agents to build result list.

    Relies entirely on ``cached_results``: an agent that finished writes its
    outcome into the outputs archive, which is downloaded and parsed during
    finalization. An agent with no cached result either is still running
    (intermediate report) or failed to publish (final report).
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

        detail = final_details.get(agent_id_str)
        test_result = cached_results.get(agent_id_str)

        if detail is None:
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
                    errored=missing_detail_errored,
                    summary_markdown=missing_detail_summary,
                )
            )
            continue

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
                    branch_name=detail.initial_branch or agent_info.branch_name,
                    test_runs=test_result.test_runs,
                )
            )
        else:
            results.append(
                TestMapReduceResult(
                    test_node_id=agent_info.test_node_id,
                    agent_name=agent_info.agent_name,
                    errored=True,
                    summary_markdown="Failed to read agent result",
                    branch_name=detail.initial_branch or agent_info.branch_name,
                )
            )

    return results


def gather_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
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
        final_details=final_details,
        timed_out_ids=timed_out_ids,
        missing_detail_errored=True,
        missing_detail_summary="Agent details not found after polling",
        cached_results=cached_results,
    )

    return [*launch_failures, *results]


def _build_current_results(
    agents: list[TestAgentInfo],
    final_details: dict[str, AgentDetails],
    timed_out_ids: set[str],
    launch_failures: Sequence[TestMapReduceResult] = (),
) -> list[TestMapReduceResult]:
    """Build current results without pulling branches, for intermediate reports.

    ``launch_failures`` are prepended so agents that failed to launch still
    appear in the report.
    """
    return [
        *launch_failures,
        *_collect_agent_results(
            agents=agents,
            final_details=final_details,
            timed_out_ids=timed_out_ids,
            missing_detail_errored=False,
            missing_detail_summary="Agent is still running...",
        ),
    ]


def wait_for_integrator(
    integrator: TestAgentInfo,
    mngr_ctx: MngrContext,
    poll_interval_seconds: float,
    host: OnlineHostInterface,
    deadline: float,
) -> str | None:
    """Poll the integrator agent until it finishes or times out."""
    agent_id_str = str(integrator.agent_id)

    while time.monotonic() < deadline:
        list_result = try_list_agents(mngr_ctx)
        if list_result is None:
            time.sleep(poll_interval_seconds)
            continue

        for agent_detail in list_result.agents:
            if str(agent_detail.id) != agent_id_str:
                continue

            if agent_detail.state == AgentLifecycleState.WAITING:
                stop_agent_on_host(host, agent_detail.id, agent_detail.name)
                return agent_detail.initial_branch

            if agent_detail.state in _TERMINAL_STATES:
                logger.info("Integrator agent finished (state={})", agent_detail.state)
                return agent_detail.initial_branch

        if try_read_integrator_outcome(integrator.work_dir, host):
            logger.info("Integrator outcome file detected, treating as done")
            return integrator.branch_name

        time.sleep(poll_interval_seconds)

    logger.warning("Integrator agent timed out, stopping it")
    stop_agent_on_host(host, AgentId(agent_id_str), integrator.agent_name)
    return None
