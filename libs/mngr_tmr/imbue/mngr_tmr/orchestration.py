"""Polling orchestration for the test-mapreduce plugin.

Drives the main map-reduce loop: poll each launched agent's volume for the
outputs archive, pull when it appears, and hand a list of ``AgentMetadata``
back to the caller. The polling cadence is the existence check on the
agent's ``outputs.tar.gz`` -- we never call ``mngr list``. Outcome JSON
parsing is the reporter's responsibility; this module treats extracted
contents as opaque.
"""

import time
from pathlib import Path

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_tmr.data_types import AgentKind
from imbue.mngr_tmr.data_types import AgentMetadata
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.launching import launch_agents_up_to_limit
from imbue.mngr_tmr.launching import stop_agent_on_host
from imbue.mngr_tmr.pulling import finalize_agent
from imbue.mngr_tmr.pulling import has_local_branch
from imbue.mngr_tmr.pulling import is_agent_outputs_ready
from imbue.mngr_tmr.report import generate_html_report
from imbue.mngr_tmr.report_upload import maybe_upload_report


def _metadata_for(info: TestAgentInfo, error_summary: str | None = None) -> AgentMetadata:
    return AgentMetadata(
        kind=AgentKind.TESTING_AGENT,
        agent_name=info.agent_name,
        test_node_id=info.test_node_id,
        branch_name=info.branch_name,
        error_summary=error_summary,
    )


def _emit_report(
    output_dir: Path | None,
    all_agents: list[TestAgentInfo],
    timed_out_ids: set[str],
    launch_failures: list[AgentMetadata],
    run_name: str,
) -> None:
    if output_dir is None:
        return
    metadata: list[AgentMetadata] = list(launch_failures)
    for info in all_agents:
        if str(info.agent_id) in timed_out_ids:
            metadata.append(_metadata_for(info, error_summary="Agent was stopped because the timeout was reached."))
        else:
            metadata.append(_metadata_for(info))
    report_path = generate_html_report(metadata, output_dir)
    # Best-effort s3 mirror of the just-written report; skipped silently
    # when AWS credentials are not configured. Logged on success so an
    # interactive user gets the URL as soon as the first poll tick fires.
    maybe_upload_report(report_path, run_name)


def launch_and_poll_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    max_agents: int,
    agent_timeout_seconds: float,
    poll_interval_seconds: float,
    output_dir: Path | None,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    launch_failures: list[AgentMetadata],
    run_name: str,
    source_dir: Path | None = None,
) -> list[AgentMetadata]:
    """Launch agents incrementally and poll until all finish.

    Handles two modes depending on arguments:

    1. Incremental launching (max_agents > 0, test_node_ids non-empty): launches
       up to max_agents at a time, polling and launching more as capacity opens.
    2. Pre-launched polling (test_node_ids empty, all_agents pre-populated):
       polls the already-launched agents without launching any new ones.

    ``all_agents``, ``all_hosts``, and ``launch_failures`` are input/output
    parameters: pre-existing entries are tracked from the start, and newly
    launched agents (or new launch failures) are appended during execution.
    Intermediate reports are written to ``output_dir/index.html`` when
    ``output_dir`` is set.

    Returns the final list of ``AgentMetadata`` (launch failures first, then
    one entry per launched agent).
    """
    remaining_tests = list(test_node_ids)
    pending_ids: set[str] = set()
    agent_id_to_info: dict[str, TestAgentInfo] = {}
    timed_out_ids: set[str] = set()
    # Used by launch_agents_up_to_limit (the batched-launch path) to
    # dedupe sanitized test names across the whole run. The non-batched
    # path doesn't enter that helper, so the set stays empty there.
    used_suffixes: set[str] = set()

    for info in all_agents:
        agent_id_str = str(info.agent_id)
        agent_id_to_info[agent_id_str] = info
        pending_ids.add(agent_id_str)

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
        "run_name": run_name,
        "used_suffixes": used_suffixes,
    }

    launch_agents_up_to_limit(**launch_kwargs)
    _emit_report(output_dir, all_agents, timed_out_ids, launch_failures, run_name)

    while pending_ids or remaining_tests:
        now = time.monotonic()
        changed = False

        for agent_id_str in list(pending_ids):
            info = agent_id_to_info[agent_id_str]
            host = all_hosts[agent_id_str]
            agent_id = AgentId(agent_id_str)

            if is_agent_outputs_ready(mngr_ctx, config.provider_name, host.id, agent_id):
                logger.info("Agent '{}' published outputs, finalizing", info.agent_name)
                finalize_agent(
                    mngr_ctx=mngr_ctx,
                    provider_name=config.provider_name,
                    host=host,
                    agent_id=agent_id,
                    agent_name=info.agent_name,
                    branch_name=info.branch_name,
                    artifact_output_dir=output_dir,
                    source_dir=source_dir,
                    cg=mngr_ctx.concurrency_group,
                    should_stop=True,
                )
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

        if changed:
            _emit_report(output_dir, all_agents, timed_out_ids, launch_failures, run_name)

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

    return [
        *launch_failures,
        *(
            _metadata_for(
                info,
                error_summary=(
                    "Agent was stopped because the timeout was reached."
                    if str(info.agent_id) in timed_out_ids
                    else None
                ),
            )
            for info in all_agents
        ),
    ]


def wait_for_integrator(
    integrator: TestAgentInfo,
    poll_interval_seconds: float,
    host: OnlineHostInterface,
    deadline: float,
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    output_dir: Path,
    source_dir: Path,
) -> str | None:
    """Poll for the integrator's outputs archive; pull, stop, and return its branch.

    The integrator publishes ``outputs.tar.gz`` the same way testing agents do.
    On success this downloads + extracts the archive under
    ``output_dir/<integrator_name>/`` and applies its ``branch.bundle`` (if any)
    onto the local repo, then returns the integrated branch name. Returns None
    when the integrator timed out or did not produce a usable branch.
    """
    pulled = False
    while time.monotonic() < deadline:
        if is_agent_outputs_ready(mngr_ctx, provider_name, host.id, integrator.agent_id):
            logger.info("Integrator outputs archive detected, finalizing")
            pulled = finalize_agent(
                mngr_ctx=mngr_ctx,
                provider_name=provider_name,
                host=host,
                agent_id=integrator.agent_id,
                agent_name=integrator.agent_name,
                branch_name=integrator.branch_name,
                artifact_output_dir=output_dir,
                source_dir=source_dir,
                cg=mngr_ctx.concurrency_group,
                should_stop=True,
            )
            break
        time.sleep(poll_interval_seconds)
    else:
        logger.warning("Integrator agent timed out, stopping it")
        stop_agent_on_host(host, integrator.agent_id, integrator.agent_name)
        return None

    if not pulled or integrator.branch_name is None:
        return None
    if not has_local_branch(source_dir, integrator.branch_name, mngr_ctx.concurrency_group):
        logger.warning("Integrator finished but produced no local branch '{}'", integrator.branch_name)
        return None
    return integrator.branch_name
