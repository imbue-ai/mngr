"""Polling loop for the mngr-mapreduce framework.

Polls each launched agent's volume for the outputs archive, extracts it on
arrival, and fires the recipe's ``on_*_finalized`` hook. The polling cadence
is the existence check on the archive file -- we never call ``mngr list``.
Archive contents are opaque to this module; the recipe's hooks are the only
place where content-aware interpretation happens.
"""

import time
from pathlib import Path

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.agent_stopper import AgentStopper
from imbue.mngr_mapreduce.data_types import AgentKind
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapReduceRecipe
from imbue.mngr_mapreduce.data_types import MapReduceTask
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.data_types import ReducerInfo
from imbue.mngr_mapreduce.launching import launch_mappers_up_to_limit
from imbue.mngr_mapreduce.pulling import is_agent_outputs_ready
from imbue.mngr_mapreduce.pulling import pull_agent_outputs


def _mapper_metadata_for(info: MapperInfo, error_summary: str | None = None) -> AgentMetadata:
    return AgentMetadata(
        kind=AgentKind.MAPPER,
        agent_name=info.agent_name,
        task_id=info.task_id,
        branch_name=info.branch_name,
        error_summary=error_summary,
    )


def _run_render_report(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    agents: list[AgentMetadata],
    reducer: AgentMetadata | None,
) -> Path | None:
    """Call ``recipe.render_report`` and return the path (or ``None`` on error).

    Recipe errors are logged but do not abort the run -- a broken renderer
    shouldn't sink an otherwise-successful run. Side effects beyond writing
    the report (e.g. mirroring it elsewhere) are the recipe's responsibility.
    """
    try:
        return recipe.render_report(ctx, agents, reducer)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Recipe render_report raised: {}", exc)
        return None


def _render_polling_tick(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    all_agents: list[MapperInfo],
    timed_out_ids: set[str],
    launch_failures: list[AgentMetadata],
) -> None:
    """Render the report for the current mid-poll state.

    Builds a fresh metadata list (launch failures + per-agent rows) so the
    recipe's renderer sees the current state, including timed-out agents
    that don't have any extracted outputs on disk.
    """
    metadata: list[AgentMetadata] = list(launch_failures)
    for info in all_agents:
        error = "Agent was stopped because the timeout was reached." if str(info.agent_id) in timed_out_ids else None
        metadata.append(_mapper_metadata_for(info, error_summary=error))
    _run_render_report(recipe, ctx, metadata, reducer=None)


def _finalize_mapper(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host: OnlineHostInterface,
    info: MapperInfo,
    stopper: AgentStopper,
) -> bool:
    """Pull a finished mapper's outputs, fire the recipe hook, and stop the agent.

    Returns True if the outputs archive was extracted (the hook fires only
    in that case). Hook exceptions are caught and logged so one mapper's
    broken hook never aborts the rest of the run. The stop is delegated to
    ``stopper`` so a slow SSH teardown doesn't serialize the polling loop.
    """
    local_dest = pull_agent_outputs(
        mngr_ctx=mngr_ctx,
        provider_name=provider_name,
        host_id=host.id,
        agent_id=info.agent_id,
        agent_name=info.agent_name,
        destination_dir=ctx.output_dir,
    )
    if local_dest is not None:
        try:
            recipe.on_mapper_finalized(ctx, local_dest, info)
        except (OSError, ValueError, RuntimeError) as exc:
            # Recipe hooks can fail for filesystem / subprocess / parsing
            # reasons; one broken hook shouldn't bring the whole run down.
            # Unexpected exception types still bubble.
            logger.warning("Recipe on_mapper_finalized raised for agent '{}': {}", info.agent_name, exc)
    stopper.submit(host, info.agent_id, info.agent_name)
    return local_dest is not None


def launch_and_poll_mappers(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    tasks: list[MapReduceTask],
    config: LaunchConfig,
    mngr_ctx: MngrContext,
    max_agents: int,
    agent_timeout_seconds: float,
    poll_interval_seconds: float,
    all_agents: list[MapperInfo],
    all_hosts: dict[str, OnlineHostInterface],
    launch_failures: list[AgentMetadata],
    stopper: AgentStopper,
) -> list[AgentMetadata]:
    """Launch mappers incrementally and poll until all finish.

    Handles two modes depending on arguments:

    1. Incremental launching (max_agents > 0, ``tasks`` non-empty): launches
       up to max_agents at a time, polling and launching more as capacity opens.
    2. Pre-launched polling (``tasks`` empty, ``all_agents`` pre-populated):
       polls the already-launched agents without launching any new ones.

    ``all_agents``, ``all_hosts``, and ``launch_failures`` are input/output
    parameters: pre-existing entries are tracked from the start, and newly
    launched agents (or new launch failures) are appended during execution.
    Intermediate reports are rendered via ``recipe.render_report`` after every
    state change.

    ``stopper`` owns the background threads for post-finalize stop calls so
    a slow ``stop_agents`` doesn't serialize the polling loop. The caller is
    responsible for ``__enter__``/``__exit__``; we just submit to it.

    Returns the final list of ``AgentMetadata`` (launch failures first, then
    one entry per launched agent).
    """
    remaining_tasks = list(tasks)
    pending_ids: set[str] = set()
    agent_id_to_info: dict[str, MapperInfo] = {}
    timed_out_ids: set[str] = set()
    # Used by launch_mappers_up_to_limit (the batched-launch path) to
    # dedupe sanitized task slugs across the whole run. The non-batched
    # path doesn't enter that helper, so the set stays empty there.
    used_suffixes: set[str] = set()

    for info in all_agents:
        agent_id_str = str(info.agent_id)
        agent_id_to_info[agent_id_str] = info
        pending_ids.add(agent_id_str)

    launch_kwargs: dict = {
        "recipe": recipe,
        "ctx": ctx,
        "remaining_tasks": remaining_tasks,
        "pending_ids": pending_ids,
        "max_agents": max_agents,
        "config": config,
        "mngr_ctx": mngr_ctx,
        "all_agents": all_agents,
        "all_hosts": all_hosts,
        "agent_id_to_info": agent_id_to_info,
        "launch_failures": launch_failures,
        "run_name": ctx.run_name,
        "used_suffixes": used_suffixes,
    }

    launch_mappers_up_to_limit(**launch_kwargs)
    _render_polling_tick(recipe, ctx, all_agents, timed_out_ids, launch_failures)

    while pending_ids or remaining_tasks:
        now = time.monotonic()
        changed = False

        for agent_id_str in list(pending_ids):
            info = agent_id_to_info[agent_id_str]
            host = all_hosts[agent_id_str]
            agent_id = AgentId(agent_id_str)

            if is_agent_outputs_ready(mngr_ctx, config.provider_name, host.id, agent_id):
                logger.info("Mapper '{}' published outputs, finalizing", info.agent_name)
                _finalize_mapper(recipe, ctx, mngr_ctx, config.provider_name, host, info, stopper)
                pending_ids.discard(agent_id_str)
                changed = True
                continue

            elapsed = now - info.created_at
            if elapsed >= agent_timeout_seconds:
                logger.warning(
                    "Mapper '{}' timed out after {:.0f}s without publishing outputs, stopping",
                    info.agent_name,
                    elapsed,
                )
                stopper.submit(host, agent_id, info.agent_name)
                pending_ids.discard(agent_id_str)
                timed_out_ids.add(agent_id_str)
                changed = True

        launch_mappers_up_to_limit(**launch_kwargs)

        if changed:
            _render_polling_tick(recipe, ctx, all_agents, timed_out_ids, launch_failures)

        if not pending_ids and not remaining_tasks:
            break

        pending_names = [agent_id_to_info[aid].agent_name for aid in pending_ids]
        queued_msg = f", {len(remaining_tasks)} queued" if remaining_tasks else ""
        logger.info(
            "Polling {} pending mapper(s){}: {}",
            len(pending_ids),
            queued_msg,
            ", ".join(str(n) for n in pending_names),
        )
        time.sleep(poll_interval_seconds)

    return [
        *launch_failures,
        *(
            _mapper_metadata_for(
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


def wait_for_reducer(
    recipe: MapReduceRecipe,
    ctx: MapReduceContext,
    info: ReducerInfo,
    host: OnlineHostInterface,
    provider_name: ProviderInstanceName,
    mngr_ctx: MngrContext,
    poll_interval_seconds: float,
    deadline: float,
    stopper: AgentStopper,
) -> AgentMetadata:
    """Poll for the reducer's outputs archive; extract, hook, and stop.

    The reducer publishes its outputs the same way mappers do. On success
    this downloads + extracts the archive under ``ctx.output_dir/<name>/``
    and fires ``recipe.on_reducer_finalized`` -- the only place the
    extracted contents are interpreted. Returns the reducer's
    ``AgentMetadata`` either way; on timeout, ``error_summary`` is set.
    """
    while time.monotonic() < deadline:
        if is_agent_outputs_ready(mngr_ctx, provider_name, host.id, info.agent_id):
            logger.info("Reducer outputs archive detected, finalizing")
            local_dest = pull_agent_outputs(
                mngr_ctx=mngr_ctx,
                provider_name=provider_name,
                host_id=host.id,
                agent_id=info.agent_id,
                agent_name=info.agent_name,
                destination_dir=ctx.output_dir,
            )
            if local_dest is not None:
                try:
                    recipe.on_reducer_finalized(ctx, local_dest, info)
                except (OSError, ValueError, RuntimeError) as exc:
                    logger.warning("Recipe on_reducer_finalized raised: {}", exc)
            stopper.submit(host, info.agent_id, info.agent_name)
            if local_dest is None:
                return AgentMetadata(
                    kind=AgentKind.REDUCER,
                    agent_name=info.agent_name,
                    branch_name=info.branch_name,
                    error_summary="Reducer published an archive but it could not be extracted.",
                )
            return AgentMetadata(
                kind=AgentKind.REDUCER,
                agent_name=info.agent_name,
                branch_name=info.branch_name,
            )
        time.sleep(poll_interval_seconds)

    logger.warning("Reducer agent timed out, stopping it")
    stopper.submit(host, info.agent_id, info.agent_name)
    return AgentMetadata(
        kind=AgentKind.REDUCER,
        agent_name=info.agent_name,
        branch_name=info.branch_name,
        error_summary="Reducer timed out before publishing outputs.",
    )
