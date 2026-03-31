from typing import Any
from typing import cast

from loguru import logger

from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.data_types import CreateAgentResult
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import DuplicateAgentNameError
from imbue.mngr.hosts.host import HostLocation
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostEnvironmentOptions
from imbue.mngr.interfaces.host import NewHostOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.utils.env_utils import parse_env_file


def _call_on_before_create_hooks(
    mngr_ctx: MngrContext,
    target_host: OnlineHostInterface | NewHostOptions,
    agent_options: CreateAgentOptions,
    create_work_dir: bool,
) -> tuple[OnlineHostInterface | NewHostOptions, CreateAgentOptions, bool]:
    """Call on_before_create hooks in a chain, passing each hook's output to the next.

    Each hook receives an OnBeforeCreateArgs containing the current values.
    If a hook returns a new OnBeforeCreateArgs, those values are used for subsequent hooks.
    If a hook returns None, the current values are passed through unchanged.

    Returns the final (possibly modified) values as a tuple.
    """
    pm = mngr_ctx.pm

    # Bundle args into the hook's expected format
    current_args: OnBeforeCreateArgs = OnBeforeCreateArgs(
        target_host=target_host,
        agent_options=agent_options,
        create_work_dir=create_work_dir,
    )

    # Get all hook implementations and call them in order, chaining results
    hookimpls = pm.hook.on_before_create.get_hookimpls()
    for hookimpl in hookimpls:
        # Call the hook with current args
        result = cast(OnBeforeCreateArgs | None, hookimpl.function(args=current_args))
        # If the hook returned a new args object, use it for subsequent hooks
        if result is not None:
            current_args = result

    # Return the final values
    return (
        current_args.target_host,
        current_args.agent_options,
        current_args.create_work_dir,
    )


@log_call
def create(
    source_location: HostLocation,
    target_host: OnlineHostInterface | NewHostOptions,
    agent_options: CreateAgentOptions,
    mngr_ctx: MngrContext,
    create_work_dir: bool = True,
    created_branch_name: str | None = None,
) -> CreateAgentResult:
    """Create and run an agent.

    This function:
    - Resolves the target host (using existing or creating new)
    - Resolves the source location to concrete host and path
    - Sets up the agent's work_dir (cloning from source if specified)
    - Creates the agent state directory
    - Runs provisioning for the agent
    - Starts the agent process
    - Returns information about the running agent and host.
    """
    # Allow plugins to modify the create arguments before we do anything else
    target_host, agent_options, create_work_dir = _call_on_before_create_hooks(
        mngr_ctx, target_host, agent_options, create_work_dir
    )

    # Run agent-type-specific preflight checks before creating the host.
    # This lets agent types fail fast on configuration errors (e.g. missing
    # gitignore entries) before expensive operations like host creation.
    agent_type = agent_options.agent_type or AgentTypeName("claude")
    resolved = resolve_agent_type(agent_type, mngr_ctx.config)
    agent_class = cast(type[AgentInterface], resolved.agent_class)
    with log_span("Running preflight checks for agent type {}", agent_type):
        agent_class.preflight_check(
            source_host=source_location.host,
            source_path=source_location.path,
            agent_options=agent_options,
            agent_config=resolved.agent_config,
            mngr_ctx=mngr_ctx,
        )

    # Determine which provider to use and get the host
    is_new_host = isinstance(target_host, NewHostOptions)
    with log_span("Resolving target host"):
        host = resolve_target_host(target_host, mngr_ctx)

    # Notify plugins that a new host was created (only for new hosts)
    if is_new_host:
        with log_span("Calling on_host_created hooks"):
            mngr_ctx.pm.hook.on_host_created(host=host, mngr_ctx=mngr_ctx)

    # while we are deploying an agent, lock the host:
    with host.lock_cooperatively():
        # Prevent duplicate agent names on the same host. The tmux session name
        # is derived from the agent name, so two agents with the same name would
        # collide on the same tmux session. This check must be inside the lock to
        # prevent TOCTOU races between concurrent create calls.
        if agent_options.name is not None:
            for existing_agent in host.get_agents():
                if existing_agent.name == agent_options.name:
                    raise DuplicateAgentNameError(agent_options.name, existing_agent.id)

        # Create the agent's work_dir on the host
        if create_work_dir:
            with log_span("Calling on_before_initial_file_copy hooks"):
                mngr_ctx.pm.hook.on_before_initial_file_copy(agent_options=agent_options, host=host)
            with log_span("Creating agent work directory from source {}", source_location.path):
                work_dir_result = host.create_agent_work_dir(source_location.host, source_location.path, agent_options)
                work_dir_path = work_dir_result.path
                created_branch_name = work_dir_result.created_branch_name
            with log_span("Calling on_after_initial_file_copy hooks"):
                mngr_ctx.pm.hook.on_after_initial_file_copy(
                    agent_options=agent_options, host=host, work_dir_path=work_dir_path
                )
        else:
            # Work dir was already created (e.g. by CLI's early copy).
            # Use target_path if set (it should contain the actual work_dir path),
            # otherwise fall back to source path (in-place mode).
            work_dir_path = (
                agent_options.target_path if agent_options.target_path is not None else source_location.path
            )

        # Create the agent state (registers the agent with the host)
        with log_span("Creating agent state in work directory {}", work_dir_path):
            agent = host.create_agent_state(work_dir_path, agent_options, created_branch_name=created_branch_name)

        # Run provisioning for the agent (hooks, dependency installation, etc.)
        with log_span("Calling on_before_provisioning hooks"):
            mngr_ctx.pm.hook.on_before_provisioning(agent=agent, host=host, mngr_ctx=mngr_ctx)
        with log_span("Provisioning agent {}", agent.name):
            host.provision_agent(agent, agent_options, mngr_ctx)
        with log_span("Calling on_after_provisioning hooks"):
            mngr_ctx.pm.hook.on_after_provisioning(agent=agent, host=host, mngr_ctx=mngr_ctx)

        # Send initial message if one is configured
        initial_message = agent.get_initial_message()
        if initial_message is not None:
            # Start agent with signal-based readiness detection
            # Raises AgentStartError if the agent doesn't signal readiness in time
            logger.info("Starting agent {} ...", agent.name)
            timeout = agent_options.ready_timeout_seconds
            agent.wait_for_ready_signal(
                is_creating=True,
                start_action=lambda: host.start_agents([agent.id]),
                timeout=timeout,
            )
            logger.info("Sending initial message...")
            agent.send_message(initial_message)
        else:
            # No initial message - just start the agent
            logger.info("Starting agent {} ...", agent.name)
            host.start_agents([agent.id])

        # Build and return the result
        result = CreateAgentResult(agent=agent, host=host)

        # Call on_agent_created hooks to notify plugins about the new agent
        with log_span("Calling on_agent_created hooks"):
            mngr_ctx.pm.hook.on_agent_created(agent=result.agent, host=result.host)

        # Emit discovery events for the host and newly created agent
        emit_discovery_events_for_host(mngr_ctx.config, host)

    return result


def _write_host_env_vars(
    host: OnlineHostInterface,
    environment: HostEnvironmentOptions,
) -> None:
    """Collect host env vars from env_files and explicit env_vars, and write to the host env file.

    Env files are read first (in order), then explicit env vars override.
    """
    if not environment.env_vars and not environment.env_files:
        return

    env_vars: dict[str, str] = {}

    # Load from env_files (earlier files are overridden by later ones)
    for env_file in environment.env_files:
        content = env_file.read_text()
        file_vars = parse_env_file(content)
        env_vars.update(file_vars)

    # Add explicit env_vars (override file-loaded values)
    for env_var in environment.env_vars:
        env_vars[env_var.key] = env_var.value

    if env_vars:
        with log_span("Writing host env vars", count=len(env_vars)):
            host.set_env_vars(env_vars)


def _apply_lifecycle_to_certified_data(
    host: OnlineHostInterface,
    lifecycle: HostLifecycleOptions,
) -> None:
    """Persist lifecycle options (idle timeout, activity sources) to the host's data.json."""
    certified_data = host.get_certified_data()
    updates: list[Any] = []
    if lifecycle.idle_timeout_seconds is not None:
        updates.append(to_update(certified_data.field_ref().idle_timeout_seconds, lifecycle.idle_timeout_seconds))
    if lifecycle.activity_sources is not None:
        updates.append(to_update(certified_data.field_ref().activity_sources, lifecycle.activity_sources))
    if updates:
        updated_data = certified_data.model_copy_update(*updates)
        host.set_certified_data(updated_data)


def resolve_target_host(
    target_host: OnlineHostInterface | NewHostOptions,
    mngr_ctx: MngrContext,
) -> OnlineHostInterface:
    """Resolve which host to use for the agent."""
    if target_host is not None and isinstance(target_host, NewHostOptions):
        # Create a new host using the specified provider
        provider = get_provider_instance(target_host.provider, mngr_ctx)
        host_name = (
            target_host.name if target_host.name is not None else provider.get_host_name(target_host.name_style)
        )

        with log_span("Calling on_before_host_create hooks"):
            mngr_ctx.pm.hook.on_before_host_create(name=host_name, provider_name=target_host.provider)
        with log_span(
            "Creating new host '{}' using provider '{}'",
            host_name,
            target_host.provider,
            tags=target_host.tags,
            build_args=target_host.build.build_args,
            start_args=target_host.build.start_args,
            lifecycle=target_host.lifecycle,
            known_hosts_count=len(target_host.environment.known_hosts),
            authorized_keys_count=len(target_host.environment.authorized_keys),
        ):
            new_host = provider.create_host(
                name=host_name,
                tags=target_host.tags,
                build_args=target_host.build.build_args,
                start_args=target_host.build.start_args,
                lifecycle=target_host.lifecycle,
                known_hosts=target_host.environment.known_hosts,
                authorized_keys=target_host.environment.authorized_keys,
                snapshot=target_host.build.snapshot,
            )

        # Persist lifecycle options (idle_timeout, activity_sources) to host data.json.
        # Some providers (e.g., Modal) handle this internally during create_host, but
        # others (e.g., local) don't, so we apply it here uniformly.
        if target_host.lifecycle is not None:
            _apply_lifecycle_to_certified_data(new_host, target_host.lifecycle)

        # Write host environment variables to the host env file (if creating a new host)
        if isinstance(target_host, NewHostOptions):
            _write_host_env_vars(new_host, target_host.environment)

        # and return it
        return new_host
    else:
        # already have the host
        logger.debug("Used existing host id={}", target_host.id)
        return target_host
