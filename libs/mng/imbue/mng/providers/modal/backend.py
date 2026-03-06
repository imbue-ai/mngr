import contextlib
from contextlib import AbstractContextManager
from io import StringIO
from pathlib import Path
from typing import ClassVar
from typing import Final

import modal
import modal.exception
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mng import hookimpl
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.errors import ConfigStructureError
from imbue.mng.errors import MngError
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.interfaces.provider_backend import ProviderBackendInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.deploy_utils import collect_provider_profile_files
from imbue.mng.providers.modal.config import ModalProviderConfig
from imbue.mng.providers.modal.instance import ModalProviderApp
from imbue.mng.providers.modal.instance import ModalProviderInstance
from imbue.mng.providers.modal.log_utils import ModalLoguruWriter
from imbue.mng.providers.modal.log_utils import enable_modal_output_capture

MODAL_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("modal")
STATE_VOLUME_SUFFIX: Final[str] = "-state"
MODAL_NAME_MAX_LENGTH: Final[int] = 64


def _create_environment(environment_name: str, cg: ConcurrencyGroup) -> None:
    """Create a Modal environment.

    Modal environments must be created before they can be used to scope resources
    like apps, volumes, and sandboxes. Since the Modal Python SDK doesn't provide
    an API for managing environments, we use the CLI.

    This function is only called when the environment is known to be missing (after
    a NotFoundError), so it does not check for existence first.
    """

    # first a quick check to make sure we're not naming things incorrectly (and making it hard to clean up these environments)
    if environment_name.startswith("mng_") and not environment_name.startswith("mng_test-"):
        raise MngError(
            f"Refusing to create Modal environment with name {environment_name}: test environments should start with 'mng_test-' and should be explicitly configured using generate_test_environment_name() so that they can be easily identified and cleaned up."
        )

    with log_span("Creating Modal environment: {}", environment_name):
        try:
            cg.run_process_to_completion(
                ["modal", "environment", "create", environment_name],
                timeout=30,
            )
            logger.info("Created Modal environment: {}", environment_name)
        except ProcessError as e:
            logger.warning("Failed to create Modal environment via CLI: {}", e)


def _lookup_persistent_app_with_env_retry(app_name: str, environment_name: str, cg: ConcurrencyGroup) -> modal.App:
    """Look up or create a persistent Modal app, retrying if the environment is not found.

    On the first NotFoundError, creates the environment and retries with exponential backoff
    to handle the race condition where Modal's API may not immediately see the newly created
    environment.
    """
    try:
        return modal.App.lookup(app_name, create_if_missing=True, environment_name=environment_name)
    except modal.exception.NotFoundError:
        # Create the environment before retrying
        _create_environment(environment_name, cg)
        return _lookup_persistent_app_with_retry(app_name, environment_name)


@retry(
    retry=retry_if_exception_type(modal.exception.NotFoundError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _lookup_persistent_app_with_retry(app_name: str, environment_name: str) -> modal.App:
    """Look up or create a persistent Modal app with tenacity retry."""
    with log_span("Retrying Modal app lookup: {} (env: {})", app_name, environment_name):
        return modal.App.lookup(app_name, create_if_missing=True, environment_name=environment_name)


def _enter_ephemeral_app_context_with_env_retry(
    app: modal.App, environment_name: str, cg: ConcurrencyGroup
) -> AbstractContextManager[modal.App]:
    """Enter an ephemeral Modal app's run context, retrying if the environment is not found.

    On the first NotFoundError, creates the environment and retries with exponential backoff
    to handle the race condition where Modal's API may not immediately see the newly created
    environment.
    """
    try:
        run_context = app.run(environment_name=environment_name)
        run_context.__enter__()
        return run_context
    except modal.exception.NotFoundError:
        # Create the environment before retrying
        _create_environment(environment_name, cg)
        return _enter_ephemeral_app_context_with_retry(app, environment_name)


@retry(
    retry=retry_if_exception_type(modal.exception.NotFoundError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _enter_ephemeral_app_context_with_retry(
    app: modal.App, environment_name: str
) -> AbstractContextManager[modal.App]:
    """Enter an ephemeral Modal app's run context with tenacity retry."""
    with log_span("Retrying Modal app context entry (env: {})", environment_name):
        run_context = app.run(environment_name=environment_name)
        run_context.__enter__()
        return run_context


class ModalAppContextHandle(FrozenModel):
    """Handle for managing a Modal app context lifecycle with output capture.

    This class captures a Modal app's run context along with the output capture
    context. The output buffer can be inspected to detect build failures and
    other issues in the Modal logs.

    Also manages the state volume for persisting host records across sandbox
    termination. The volume is created lazily when first accessed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_context: AbstractContextManager[modal.App] | None = Field(
        description="The Modal app.run() context manager (only present for ephemeral apps)"
    )
    app_name: str = Field(description="The name of the Modal app")
    environment_name: str = Field(description="The Modal environment name for user isolation")
    output_capture_context: AbstractContextManager[tuple[StringIO, ModalLoguruWriter | None]] = Field(
        description="The output capture context manager"
    )
    output_buffer: StringIO = Field(description="StringIO buffer containing captured Modal output")
    loguru_writer: ModalLoguruWriter | None = Field(description="Loguru writer for structured logging (or None)")
    volume_name: str = Field(description="Name of the state volume for persisting host records")
    volume: modal.Volume | None = Field(
        default=None, description="The Modal volume for state storage (lazily created)"
    )


def _exit_modal_app_context(handle: ModalAppContextHandle) -> None:
    """Exit a Modal app context and its output capture context."""
    with log_span("Exiting Modal app context: {}", handle.app_name):
        # Log any captured output for debugging
        captured_output = handle.output_buffer.getvalue()
        if captured_output:
            logger.trace("Captured Modal output ({} chars): {}", len(captured_output), captured_output[:500])

        # Exit the app context first
        try:
            if handle.run_context is not None:
                handle.run_context.__exit__(None, None, None)
        except modal.exception.Error as e:
            logger.warning("Modal error exiting app context {}: {}", handle.app_name, e)

        # Exit the output capture context - this is a cleanup operation so we just
        # suppress any errors
        with contextlib.suppress(OSError, RuntimeError):
            handle.output_capture_context.__exit__(None, None, None)


class ModalProviderBackend(ProviderBackendInterface):
    """Backend for creating Modal sandbox provider instances.

    The Modal provider backend creates provider instances that manage Modal sandboxes
    as hosts. Each sandbox runs sshd and is accessed via SSH/pyinfra.

    This class maintains a class-level registry of Modal app contexts by app name.
    This ensures we only create one app per unique app_name, even if multiple
    ModalProviderInstance objects are created with the same app_name.
    """

    # Class-level registry of app contexts by app name.
    # Maps app_name -> (modal.App, ModalAppContextHandle)
    _app_registry: ClassVar[dict[str, tuple[modal.App, ModalAppContextHandle]]] = {}

    @classmethod
    def _get_or_create_app(
        cls, app_name: str, environment_name: str, is_persistent: bool, cg: ConcurrencyGroup
    ) -> tuple[modal.App, ModalAppContextHandle]:
        """Get or create a Modal app with output capture.

        Creates an ephemeral app with `modal.App(name)` and enters its `app.run()`
        context manager. The app is cached in the class-level registry by name, so
        multiple calls with the same app_name will return the same app.

        Modal output is captured via enable_modal_output_capture(), which routes
        all Modal logs to both a StringIO buffer (for inspection) and to loguru
        (for mng's logging system).

        Also prepares the volume name for state storage. The volume is created
        lazily when first accessed via get_volume_for_app().

        The environment_name is used to scope all Modal resources (apps, volumes,
        sandboxes) to a specific user, enabling isolation between different mng
        installations sharing the same Modal account.

        Raises modal.exception.AuthError if Modal credentials are not configured.
        """
        if app_name in cls._app_registry:
            return cls._app_registry[app_name]

        with log_span("Creating ephemeral Modal app with output capture: {} (env: {})", app_name, environment_name):
            # Enter the output capture context first
            output_capture_context = enable_modal_output_capture(is_logging_to_loguru=True)
            output_buffer, loguru_writer = output_capture_context.__enter__()

            if is_persistent:
                app = _lookup_persistent_app_with_env_retry(app_name, environment_name, cg)
                run_context = None
            else:
                # Create the Modal app
                app = modal.App(app_name)

                # Enter the app.run() context manager manually so we can return the app
                # while keeping the context active until close() is called
                run_context = _enter_ephemeral_app_context_with_env_retry(app, environment_name, cg)

            # Set app metadata on the loguru writer for structured logging
            if loguru_writer is not None:
                loguru_writer.app_id = app.app_id
                loguru_writer.app_name = app.name

            # Create the volume name for state storage (volume created lazily)
            volume_name = f"{app_name}{STATE_VOLUME_SUFFIX}"

            context_handle = ModalAppContextHandle(
                run_context=run_context,
                app_name=app_name,
                environment_name=environment_name,
                output_capture_context=output_capture_context,
                output_buffer=output_buffer,
                loguru_writer=loguru_writer,
                volume_name=volume_name,
                volume=None,
            )
            cls._app_registry[app_name] = (app, context_handle)
        return app, context_handle

    @classmethod
    def get_volume_for_app(cls, app_name: str) -> modal.Volume:
        """Get or create the state volume for an app.

        The volume is used to persist host records (including snapshots) across
        sandbox termination. This allows multiple mng instances to share state
        and enables restoration from snapshots even after the original sandbox
        is gone.

        The volume is created lazily on first access and cached in the context
        handle for subsequent calls. The volume is scoped to the same environment
        as the app.

        Raises MngError if the app has not been created yet.
        """
        if app_name not in cls._app_registry:
            raise MngError(f"App {app_name} not found in registry")

        _, context_handle = cls._app_registry[app_name]

        # Return cached volume if already created
        if context_handle.volume is not None:
            return context_handle.volume

        # Create or get the volume in the same environment as the app
        with log_span(
            "Ensuring state volume: {} (env: {})", context_handle.volume_name, context_handle.environment_name
        ):
            volume = modal.Volume.from_name(
                context_handle.volume_name,
                create_if_missing=True,
                environment_name=context_handle.environment_name,
                version=2,
            )

        # Cache the volume in the context handle (need to update the registry entry)
        # Since FrozenModel is immutable, we need to create a new handle
        updated_handle = ModalAppContextHandle(
            run_context=context_handle.run_context,
            app_name=context_handle.app_name,
            environment_name=context_handle.environment_name,
            output_capture_context=context_handle.output_capture_context,
            output_buffer=context_handle.output_buffer,
            loguru_writer=context_handle.loguru_writer,
            volume_name=context_handle.volume_name,
            volume=volume,
        )
        app, _ = cls._app_registry[app_name]
        cls._app_registry[app_name] = (app, updated_handle)

        return volume

    @classmethod
    def close_app(cls, app_name: str) -> None:
        """Close a Modal app context.

        Exits the app.run() context manager and removes the app from the registry.
        This makes the app ephemeral and prevents accumulation.
        """
        if app_name in cls._app_registry:
            _, context_handle = cls._app_registry.pop(app_name)
            _exit_modal_app_context(context_handle)

    @classmethod
    def reset_app_registry(cls) -> None:
        """Reset the modal app registry.

        Closes all open app contexts and clears the registry. This is primarily used
        for test isolation to ensure a clean state between tests.
        """
        for app_name, (_, context_handle) in list(cls._app_registry.items()):
            try:
                _exit_modal_app_context(context_handle)
            except modal.exception.Error as e:
                logger.warning("Modal error closing app {} during reset: {}", app_name, e)
        cls._app_registry.clear()

    @staticmethod
    def get_name() -> ProviderBackendName:
        return MODAL_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Modal cloud sandboxes with SSH access"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ModalProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return """\
Supported build arguments for the modal provider:
  --file PATH           Path to the Dockerfile to build the sandbox image. Default: Dockerfile in context dir
  --context-dir PATH    Build context directory for Dockerfile COPY/ADD instructions. Default: Dockerfile's directory
  --cpu COUNT           Number of CPU cores (0.25-16). Default: 1.0
  --memory GB           Memory in GB (0.5-32). Default: 1.0
  --gpu TYPE            GPU type to use (e.g., t4, a10g, a100, any). Default: no GPU
  --image NAME          Base Docker image to use. Not required if using --file. Default: debian:bookworm-slim
  --timeout SEC         Maximum sandbox lifetime in seconds. Default: 900 (15 min)
  --region NAME         Region to run the sandbox in (e.g., us-east, us-west, eu-west). Default: auto
  --secret VAR          Pass an environment variable as a secret to the image build. The value of
                        VAR is read from your current environment and made available during Dockerfile
                        RUN commands via --mount=type=secret,id=VAR. Can be specified multiple times.
  --offline             Block all outbound network access from the sandbox [experimental]. Default: off
  --cidr-allowlist CIDR Restrict network access to the specified CIDR range (e.g., 203.0.113.0/24) [experimental].
                        Can be specified multiple times.
  --volume NAME:PATH    Mount a persistent Modal Volume at PATH inside the sandbox [experimental]. NAME is the
                        volume name on Modal (created if it doesn't exist). Can be specified
                        multiple times.
  --docker-build-arg KEY=VALUE
                        Override a Dockerfile ARG default value. For example,
                        --docker-build-arg=CLAUDE_CODE_VERSION=2.1.50 sets the CLAUDE_CODE_VERSION
                        ARG during the image build. Can be specified multiple times.
"""

    @staticmethod
    def get_start_args_help() -> str:
        return "No start arguments are supported for the modal provider."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mng_ctx: MngContext,
    ) -> ProviderInstanceInterface:
        """Build a Modal provider instance."""
        if not isinstance(config, ModalProviderConfig):
            raise ConfigStructureError(f"Expected ModalProviderConfig, got {type(config).__name__}")

        # Use prefix + user_id for the environment name, ensuring isolation
        # between different mng installations sharing the same Modal account.
        # The app name is just prefix + name (no user_id).
        # The provider config can override the profile's user_id to allow sharing
        # Modal resources across different profiles or installations.
        prefix = mng_ctx.config.prefix
        user_id = config.user_id if config.user_id is not None else mng_ctx.get_profile_user_id()
        environment_name = f"{prefix}{user_id}"
        default_app_name = f"{prefix}{name}"

        # Truncate environment_name if needed to fit Modal's 64 char limit
        if len(environment_name) > MODAL_NAME_MAX_LENGTH:
            logger.warning(
                "Truncating Modal environment name to {} characters: {}", MODAL_NAME_MAX_LENGTH, environment_name
            )
            environment_name = environment_name[:MODAL_NAME_MAX_LENGTH]

        app_name = config.app_name if config.app_name is not None else default_app_name
        host_dir = config.host_dir if config.host_dir is not None else Path("/mng")

        # Truncate app_name if needed to fit Modal's 64 char limit (accounting for volume suffix)
        max_app_name_length = MODAL_NAME_MAX_LENGTH - len(STATE_VOLUME_SUFFIX)
        if len(app_name) > max_app_name_length:
            logger.warning("Truncating Modal app name to {} characters: {}", max_app_name_length, app_name)
            app_name = app_name[:max_app_name_length]

        # Create the ModalProviderApp that manages the Modal app and its resources
        try:
            app, context_handle = ModalProviderBackend._get_or_create_app(
                app_name, environment_name, config.is_persistent, mng_ctx.concurrency_group
            )
            volume = ModalProviderBackend.get_volume_for_app(app_name)

            modal_app = ModalProviderApp(
                app_name=app_name,
                environment_name=environment_name,
                app=app,
                volume=volume,
                close_callback=lambda: ModalProviderBackend.close_app(app_name),
                get_output_callback=lambda: context_handle.output_buffer.getvalue(),
            )
        except modal.exception.AuthError as e:
            raise MngError(
                "Modal is not authorized: run 'uvx modal token set' to authenticate, or disable this provider with "
                f"'mng config set --scope local providers.{name}.is_enabled false'. (original error: {e})",
            ) from e

        return ModalProviderInstance(
            name=name,
            host_dir=host_dir,
            mng_ctx=mng_ctx,
            config=config,
            modal_app=modal_app,
        )


# SSH key and host key file names stored in the modal provider's profile directory.
# These are generated by load_or_create_ssh_keypair() and should not be baked into deployment images.
# Note that it is ok to include the host keys, since those are already present remotely (that's the whole point)
_MODAL_EXCLUDED_PROFILE_FILES: Final[frozenset[str]] = frozenset(
    {
        "modal_ssh_key",
        "modal_ssh_key.pub",
        "known_hosts",
    }
)


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the Modal provider backend."""
    return (ModalProviderBackend, ModalProviderConfig)


@hookimpl
def get_files_for_deploy(
    mng_ctx: MngContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """Include modal provider profile files, excluding SSH keypairs.

    SSH keypairs (modal_ssh_key, host_key, and their .pub companions) and
    known_hosts are excluded because they are environment-specific secrets.
    The deployed environment generates fresh keypairs via
    load_or_create_ssh_keypair().
    """
    if not include_user_settings:
        return {}
    return collect_provider_profile_files(mng_ctx, "modal", _MODAL_EXCLUDED_PROFILE_FILES)


@hookimpl
def on_agent_created(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """We need to snapshot the sandbox after the agents are created and initial messages are delivered."""

    if not isinstance(host, Host):
        raise MngError("Host is not an instance of Host class")

    provider_instance = host.provider_instance
    if isinstance(provider_instance, ModalProviderInstance):
        provider_instance.on_agent_created(agent, host)
