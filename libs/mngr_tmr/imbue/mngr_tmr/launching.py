"""Agent and host launching for the test-mapreduce plugin."""

import math
import time
from concurrent.futures import Future
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.create import resolve_target_host
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.api.sync import rsync_to_remote
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import AgentDataOptions
from imbue.mngr.interfaces.host import AgentGitOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import NewHostBuildOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import TransferMode
from imbue.mngr.primitives import UncommittedChangesMode
from imbue.mngr_tmr.data_types import AgentKind
from imbue.mngr_tmr.data_types import AgentMetadata
from imbue.mngr_tmr.data_types import TestAgentInfo
from imbue.mngr_tmr.data_types import TmrLaunchConfig
from imbue.mngr_tmr.prompts import INTEGRATOR_INPUTS_DIRNAME
from imbue.mngr_tmr.prompts import build_integrator_prompt
from imbue.mngr_tmr.prompts import build_test_agent_prompt
from imbue.mngr_tmr.utils import dedup_name
from imbue.mngr_tmr.utils import resolve_templates
from imbue.mngr_tmr.utils import sanitize_test_name_for_agent

_AGENT_CREATION_TIMEOUT_SECONDS = 600.0

# Hardcoded location of the code repo on every snapshotter-built host. The
# snapshotter agent's work_dir is placed here so that subsequent agents created
# from the snapshot can git-worktree off this same-host checkout instead of
# re-uploading the source from the laptop.
_HOST_CODE_DIR = Path("/code")


def _make_test_agent_identity(run_name: str, test_node_id: str, used_suffixes: set[str]) -> tuple[AgentName, str]:
    """Generate (agent_name, branch_name) for a test agent.

    The names are derived deterministically from ``(run_name,
    test_node_id)`` so the launch attempt and any failure-report entry
    share the same name. ``used_suffixes`` is mutated with the suffix
    chosen for this test; if sanitization-truncation would have collapsed
    two distinct test node ids onto the same suffix, ``-2`` / ``-3`` /
    ... is appended to keep names unique within the run.
    """
    sanitized = sanitize_test_name_for_agent(test_node_id)
    suffix = dedup_name(sanitized, used_suffixes)
    return AgentName(f"tmr-{run_name}-{suffix}"), f"mngr-tmr/{run_name}/{suffix}"


def _make_launch_failure_metadata(
    test_node_id: str, agent_name: AgentName, branch_name: str, error: object
) -> AgentMetadata:
    """Build an AgentMetadata marking that an agent failed to launch.

    Used so launch failures still appear in the HTML report (as errored
    entries in the FAILED section) instead of silently disappearing.
    ``agent_name`` should be the name that was used for the launch
    attempt, so the report row matches the host/tmux session if the
    user retained it for debugging.
    """
    return AgentMetadata(
        kind=AgentKind.TESTING_AGENT,
        agent_name=agent_name,
        test_node_id=test_node_id,
        branch_name=branch_name,
        error_summary=f"Failed to launch agent: {error}",
    )


def _resolve_build_options(config: TmrLaunchConfig, mngr_ctx: MngrContext) -> NewHostBuildOptions:
    """Resolve templates and build NewHostBuildOptions for a tmr agent or host pool entry."""
    tmpl = resolve_templates(config.templates, mngr_ctx.config) if config.templates else {}
    raw_build_args = tmpl.get("build_args", ())
    raw_start_args = tmpl.get("start_args", ())
    build_args = tuple(str(a) for a in raw_build_args) if isinstance(raw_build_args, (list, tuple)) else ()
    start_args = tuple(str(a) for a in raw_start_args) if isinstance(raw_start_args, (list, tuple)) else ()
    return NewHostBuildOptions(snapshot=config.snapshot, build_args=build_args, start_args=start_args)


def _build_host_environment(config: TmrLaunchConfig) -> HostEnvironmentOptions:
    """Build HostEnvironmentOptions for hosts created by tmr."""
    return HostEnvironmentOptions(authorized_keys=config.additional_authorized_keys)


# Server-side label that classifies a TMR-launched agent. The value is
# always an AgentKind enum value so the in-process kind and the on-server
# label have a single source of truth -- mngr ls --include
# 'labels.tmr_role == "INTEGRATOR"' matches the in-process classification.
_ROLE_LABEL_KEY = "tmr_role"


def _build_agent_options(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    kind: AgentKind,
    initial_message: str | None = None,
    target_path: Path | None = None,
    transfer_mode: TransferMode = TransferMode.GIT_MIRROR,
) -> CreateAgentOptions:
    """Build CreateAgentOptions for a tmr agent.

    ``kind`` is stamped onto ``label_options`` as the ``tmr_role`` label,
    overriding any prior value carried on ``config``.

    ``target_path`` overrides where the agent's work_dir is placed on the
    host (used to pin the snapshotter to ``/code``). ``transfer_mode``
    defaults to ``GIT_MIRROR`` for every provider (including local) so that
    agent branches are kept in the agent's own clone and surface in the
    source repo only via the published bundle -- the orchestrator code path
    is then identical across providers. Callers override to ``GIT_WORKTREE``
    when source and target live on the same host (e.g. when an agent built
    from a snapshot sources from the host's own ``/code``).
    """
    is_remote = config.provider_name.lower() != LOCAL_PROVIDER_NAME
    label_options = AgentLabelOptions(labels={**config.label_options.labels, _ROLE_LABEL_KEY: kind.value})
    return CreateAgentOptions(
        agent_type=config.agent_type,
        name=agent_name,
        initial_message=initial_message,
        target_path=target_path,
        transfer_mode=transfer_mode,
        git=AgentGitOptions(
            base_branch=config.base_commit,
            new_branch_name=branch_name,
        ),
        data_options=AgentDataOptions(is_rsync_enabled=False),
        environment=config.env_options,
        label_options=label_options,
        ready_timeout_seconds=60.0 if is_remote else 10.0,
    )


def _create_tmr_agent(
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    kind: AgentKind,
    initial_message: str | None = None,
    existing_host: OnlineHostInterface | None = None,
    host_name: HostName | None = None,
) -> CreateAgentResult:
    """Create an agent on the configured provider with an optional initial message.

    ``kind`` classifies the agent -- it controls the ``tmr_role`` label
    (set by ``_build_agent_options``) and triggers the snapshotter-specific
    work_dir pinning when ``kind is AgentKind.SNAPSHOTTER``.

    If existing_host is provided, the agent is placed on that host instead of
    creating a new one (used for host sharing in remote providers).

    Snapshotter agents have their work_dir pinned to ``/code`` so the git
    repo on the snapshotter's host lives at a known path; subsequent agents
    created from the snapshot then git-worktree off that path instead of
    re-uploading the source.

    If the agent is being placed on an existing host built from a snapshot
    (``existing_host`` is set and ``config.snapshot`` is set), source from
    that host's ``/code`` via git-worktree -- avoiding network roundtrips
    to upload code that's already there.
    """
    if existing_host is not None:
        target_host: OnlineHostInterface | NewHostOptions = existing_host
    elif config.provider_name.lower() == LOCAL_PROVIDER_NAME:
        # The local provider has a single fixed host ("localhost"); reuse the
        # source_host (already the local host) instead of the new-host path.
        # That path would call _generate_unique_host_name, which never finds
        # a free name because the local provider's get_host_name always
        # returns "localhost" and discover_hosts always reports it as taken.
        target_host = config.source_host
    else:
        build = _resolve_build_options(config, mngr_ctx)
        target_host = NewHostOptions(
            provider=config.provider_name,
            name=host_name,
            build=build,
            environment=_build_host_environment(config),
        )

    if kind is AgentKind.SNAPSHOTTER:
        source_location = HostLocation(host=config.source_host, path=config.source_dir)
        agent_options = _build_agent_options(
            agent_name,
            branch_name,
            config,
            kind,
            initial_message=initial_message,
            target_path=_HOST_CODE_DIR,
        )
    elif existing_host is not None and config.snapshot is not None:
        source_location = HostLocation(host=existing_host, path=_HOST_CODE_DIR)
        agent_options = _build_agent_options(
            agent_name,
            branch_name,
            config,
            kind,
            initial_message=initial_message,
            transfer_mode=TransferMode.GIT_WORKTREE,
        )
    else:
        source_location = HostLocation(host=config.source_host, path=config.source_dir)
        agent_options = _build_agent_options(agent_name, branch_name, config, kind, initial_message=initial_message)

    return api_create(
        source_location=source_location,
        target_host=target_host,
        agent_options=agent_options,
        mngr_ctx=mngr_ctx,
    )


def _launch_test_agent(
    test_node_id: str,
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
    existing_host: OnlineHostInterface | None = None,
    host_name: HostName | None = None,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch a single agent to run and optionally fix one test.

    ``agent_name`` and ``branch_name`` are passed in (rather than derived
    from ``test_node_id`` here) so the caller can reuse the same identity
    when reporting a launch failure.
    """
    logger.info("Launching agent '{}' for test: {}", agent_name, test_node_id)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=branch_name,
        config=config,
        mngr_ctx=mngr_ctx,
        kind=AgentKind.TESTING_AGENT,
        initial_message=build_test_agent_prompt(test_node_id, pytest_flags, prompt_suffix),
        existing_host=existing_host,
        host_name=host_name,
    )

    return (
        TestAgentInfo(
            test_node_id=test_node_id,
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            work_dir=create_result.agent.work_dir,
            branch_name=branch_name,
            created_at=time.monotonic(),
        ),
        create_result.host,
    )


def _create_snapshot_host(
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
) -> SnapshotName:
    """Launch a dedicated snapshotter agent, snapshot its host, then stop it."""
    agent_name = AgentName(f"tmr-{run_name}-snapshotter")
    host_name = HostName(f"tmr-{run_name}-snapshotter")

    logger.info("Launching snapshotter agent '{}' for provisioning...", agent_name)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=f"mngr-tmr/{run_name}/snapshotter",
        config=config,
        mngr_ctx=mngr_ctx,
        kind=AgentKind.SNAPSHOTTER,
        host_name=host_name,
    )

    snapshotter_host = create_result.host
    snapshotter_agent_id = create_result.agent.id

    try:
        provider = get_provider_instance(config.provider_name, mngr_ctx)
        snapshot_id = provider.create_snapshot(snapshotter_host)
        snapshot_name = SnapshotName(str(snapshot_id))
        logger.info("Created snapshot '{}' from snapshotter host", snapshot_name)
        return snapshot_name
    finally:
        stop_agent_on_host(snapshotter_host, snapshotter_agent_id, agent_name)


def stop_agent_on_host(host: OnlineHostInterface, agent_id: AgentId, agent_name: AgentName) -> None:
    """Stop a single agent on the host."""
    try:
        host.stop_agents([agent_id])
        logger.info("Stopped agent '{}'", agent_name)
    except (MngrError, HostError) as exc:
        logger.warning("Failed to stop agent '{}': {}", agent_name, exc)


def _create_host_pool(
    host_count: int,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
    max_parallel: int,
) -> list[OnlineHostInterface]:
    """Pre-create a pool of hosts for remote agent placement."""
    hosts: list[OnlineHostInterface] = []
    build = _resolve_build_options(config, mngr_ctx)

    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
        name="tmr_create_hosts",
        max_workers=max_parallel,
    ) as executor:
        futures = []
        host_environment = _build_host_environment(config)
        for i in range(host_count):
            h_name = HostName(f"tmr-{run_name}-host-{i}")
            new_host_opts = NewHostOptions(
                provider=config.provider_name,
                name=h_name,
                build=build,
                environment=host_environment,
            )
            futures.append(executor.submit(resolve_target_host, new_host_opts, mngr_ctx))
        for future in futures:
            try:
                hosts.append(future.result())
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create host: {}", exc)

    logger.info("Created {} host(s) for agent placement", len(hosts))
    return hosts


def launch_all_test_agents(
    test_node_ids: list[str],
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    launch_failures: list[AgentMetadata],
    run_name: str,
    prompt_suffix: str = "",
    max_parallel: int = 4,
    launch_delay_seconds: float = 2.0,
    agents_per_host: int = 4,
) -> tuple[list[TestAgentInfo], dict[str, OnlineHostInterface], SnapshotName | None]:
    """Launch agents for all collected tests.

    For remote providers, agents_per_host controls how many agents share a single
    host. Hosts are pre-created in a pool and agents are assigned round-robin.
    For local providers, this setting is ignored (all agents share localhost).

    When the provider supports snapshots (e.g. modal) and the caller has not
    pre-supplied one via ``config.snapshot``, a snapshot host is built first
    and the resulting snapshot ID is propagated into ``launch_config`` so all
    other agents are launched from it. Providers without snapshot support
    (local, docker, ...) skip this step silently.

    Per-test launch failures are appended (in place) to ``launch_failures`` so
    they can be surfaced in the report.
    """
    agents: list[TestAgentInfo] = []
    agent_hosts: dict[str, OnlineHostInterface] = {}

    launch_config = config
    if config.snapshot is None:
        # Pass is_for_host_creation=True so a backend with one-time bootstrap
        # (Modal's per-user environment) creates that resource here. The
        # snapshotter and every test agent that follows is a host creation,
        # so this is the moment to allow bootstrap; without it, snapshotting
        # against a fresh Modal account aborts with ProviderEmptyError before
        # any host is created.
        provider = get_provider_instance(config.provider_name, mngr_ctx, is_for_host_creation=True)
        if provider.supports_snapshots:
            try:
                snapshot_name = _create_snapshot_host(config, mngr_ctx, run_name)
                launch_config = config.model_copy_update(to_update(config.field_ref().snapshot, snapshot_name))
            except (MngrError, HostError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to create snapshot, launching agents without snapshot: {}", exc)

    is_local = launch_config.provider_name.lower() == LOCAL_PROVIDER_NAME
    host_pool: list[OnlineHostInterface] = []
    if not is_local and agents_per_host > 0:
        host_count = math.ceil(len(test_node_ids) / agents_per_host)
        if host_count > 0:
            host_pool = _create_host_pool(host_count, launch_config, mngr_ctx, run_name, max_parallel)

    used_suffixes: set[str] = set()
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group,
        name="tmr_launch",
        max_workers=max_parallel,
    ) as executor:
        futures: list[tuple[Future[tuple[TestAgentInfo, OnlineHostInterface]], str, AgentName, str]] = []
        for i, test_node_id in enumerate(test_node_ids):
            if i > 0 and launch_delay_seconds > 0:
                time.sleep(launch_delay_seconds)
            existing_host = host_pool[i % len(host_pool)] if host_pool else None
            h_name = HostName(f"tmr-{run_name}-host-{i}") if not is_local and not host_pool else None
            agent_name, branch_name = _make_test_agent_identity(run_name, test_node_id, used_suffixes)
            futures.append(
                (
                    executor.submit(
                        _launch_test_agent,
                        test_node_id,
                        agent_name,
                        branch_name,
                        launch_config,
                        mngr_ctx,
                        pytest_flags,
                        prompt_suffix,
                        existing_host,
                        h_name,
                    ),
                    test_node_id,
                    agent_name,
                    branch_name,
                )
            )
        for future, test_node_id, agent_name, branch_name in futures:
            try:
                info, host = future.result()
                agents.append(info)
                agent_hosts[str(info.agent_id)] = host
            except (MngrError, HostError, AgentError, OSError, BaseExceptionGroup) as exc:
                logger.warning("Failed to launch agent for {}: {}", test_node_id, exc)
                launch_failures.append(_make_launch_failure_metadata(test_node_id, agent_name, branch_name, exc))

    logger.info("Launched {} agent(s)", len(agents))
    return agents, agent_hosts, launch_config.snapshot


def _launch_with_timeout(
    test_node_id: str,
    agent_name: AgentName,
    branch_name: str,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch a test agent with a timeout. Raises TimeoutError if creation takes too long."""
    with ConcurrencyGroupExecutor(mngr_ctx.concurrency_group, name="launch-agent", max_workers=1) as executor:
        future = executor.submit(
            _launch_test_agent, test_node_id, agent_name, branch_name, config, mngr_ctx, pytest_flags, prompt_suffix
        )
        return future.result(timeout=_AGENT_CREATION_TIMEOUT_SECONDS)


def launch_agents_up_to_limit(
    remaining_tests: list[str],
    pending_ids: set[str],
    max_agents: int,
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str,
    all_agents: list[TestAgentInfo],
    all_hosts: dict[str, OnlineHostInterface],
    agent_id_to_info: dict[str, TestAgentInfo],
    launch_failures: list[AgentMetadata],
    run_name: str,
    used_suffixes: set[str],
) -> None:
    """Launch agents from remaining_tests until we hit max_agents running.

    Mutates remaining_tests (pops from front), pending_ids, all_agents,
    all_hosts, agent_id_to_info, ``used_suffixes``, and launch_failures
    in place. Per-test launch failures are appended to ``launch_failures``
    so they can be surfaced in the report.
    """
    while remaining_tests and (max_agents <= 0 or len(pending_ids) < max_agents):
        test_node_id = remaining_tests.pop(0)
        agent_name, branch_name = _make_test_agent_identity(run_name, test_node_id, used_suffixes)
        try:
            info, host = _launch_with_timeout(
                test_node_id, agent_name, branch_name, config, mngr_ctx, pytest_flags, prompt_suffix
            )
        except TimeoutError:
            logger.warning("Agent creation timed out after {}s for {}", _AGENT_CREATION_TIMEOUT_SECONDS, test_node_id)
            launch_failures.append(
                _make_launch_failure_metadata(
                    test_node_id,
                    agent_name,
                    branch_name,
                    f"creation timed out after {_AGENT_CREATION_TIMEOUT_SECONDS}s",
                )
            )
            continue
        except (MngrError, HostError, AgentError, OSError, BaseExceptionGroup) as exc:
            logger.warning("Failed to launch agent for {}: {}", test_node_id, exc)
            launch_failures.append(_make_launch_failure_metadata(test_node_id, agent_name, branch_name, exc))
            continue
        all_agents.append(info)
        all_hosts[str(info.agent_id)] = host
        agent_id_to_info[str(info.agent_id)] = info
        pending_ids.add(str(info.agent_id))


def launch_integrator_agent(
    config: TmrLaunchConfig,
    mngr_ctx: MngrContext,
    run_name: str,
    output_dir: Path,
) -> tuple[TestAgentInfo, OnlineHostInterface]:
    """Launch an integrator agent that cherry-picks fix branches into a linear stack.

    Test agents always run with ``GIT_MIRROR`` transfer mode (see
    ``_build_agent_options``), so their branches never appear in the
    orchestrator's source repo automatically -- the only way the integrator
    gets at them is via the per-agent ``branch.bundle`` files that the
    orchestrator has already pulled and extracted under ``output_dir``. The
    integrator host has no knowledge of those branches either, so we rsync
    ``output_dir`` into ``<work_dir>/INTEGRATOR_INPUTS_DIRNAME/`` and deliver
    the integrator prompt via ``send_message``. The prompt walks that input
    directory, applies the should-pull predicate to filter, fetches the
    qualifying bundles into local branches, and cherry-picks.
    """
    agent_name = AgentName(f"tmr-{run_name}-integrator")
    branch_name = f"mngr-tmr/{run_name}/integrated"
    host_name = HostName(f"tmr-{run_name}-integrator")

    logger.info("Launching integrator agent '{}'", agent_name)
    create_result = _create_tmr_agent(
        agent_name=agent_name,
        branch_name=branch_name,
        config=config,
        mngr_ctx=mngr_ctx,
        kind=AgentKind.INTEGRATOR,
        initial_message=None,
        host_name=host_name,
    )

    destination = create_result.agent.work_dir / INTEGRATOR_INPUTS_DIRNAME
    logger.info("Rsyncing integrator inputs to '{}:{}'", create_result.host.id, destination)
    rsync_to_remote(
        local_path=output_dir,
        remote_host=create_result.host,
        remote_path=destination,
        is_dry_run=False,
        is_delete=False,
        uncommitted_changes=UncommittedChangesMode.CLOBBER,
        cg=mngr_ctx.concurrency_group,
    )
    logger.info("Sending integrator prompt to '{}'", agent_name)
    create_result.agent.send_message(build_integrator_prompt())

    return (
        TestAgentInfo(
            test_node_id="integrator",
            agent_id=create_result.agent.id,
            agent_name=create_result.agent.name,
            work_dir=create_result.agent.work_dir,
            branch_name=branch_name,
            created_at=time.monotonic(),
        ),
        create_result.host,
    )
