import contextlib
import os
from contextlib import AbstractContextManager
from io import StringIO
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.deploy_utils import collect_provider_profile_files
from imbue.mngr.utils.env_utils import TEST_ENV_PATTERN
from imbue.mngr_modal import hookimpl
from imbue.mngr_modal.config import ModalMode
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.instance import ModalProviderApp
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.modal_proxy.direct import DirectModalInterface
from imbue.modal_proxy.errors import ModalProxyAuthError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ModalInterface
from imbue.modal_proxy.interface import VolumeInterface
from imbue.modal_proxy.log_utils import ModalLoguruWriter

MODAL_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("modal")
STATE_VOLUME_SUFFIX: Final[str] = "-state"
MODAL_NAME_MAX_LENGTH: Final[int] = 64


def truncate_modal_name(name: str, max_length: int) -> str:
    """Truncate a name to Modal's length limit, stripping trailing separators.

    Shared by the create path (backend) and the test delete path (e2e conftest)
    so both arrive at the same env name from the same inputs.
    """
    if len(name) <= max_length:
        return name
    return name[:max_length].rstrip("-_")


def _create_environment(environment_name: str, modal_interface: ModalInterface) -> None:
    """Create a Modal environment.

    Modal environments must be created before they can be used to scope resources
    like apps, volumes, and sandboxes.

    Called from the NotFoundError retry path and does not pre-check for existence.
    Any failure from ``modal environment create`` -- including a concurrent
    creation that races and causes an "already exists" response -- is surfaced
    as a MngrError. Callers should not call this unless they have evidence the
    environment is missing.
    """

    # first a quick check to make sure we're not naming things incorrectly (and making it hard to clean up these environments)
    if environment_name.startswith("mngr_") and not environment_name.startswith("mngr_test-"):
        raise MngrError(
            f"Refusing to create Modal environment with name {environment_name}: test environments should start with 'mngr_test-' and should be explicitly configured using generate_test_environment_name() so that they can be easily identified and cleaned up."
        )

    # Second line of defense: when running under pytest, require the env name to
    # match the timestamped `mngr_test-YYYY-MM-DD-HH-MM-SS` pattern (same
    # TEST_ENV_PATTERN used by cleanup_old_modal_test_environments.py and
    # modal_mngr_ctx). Without this, a test that spawns `mngr` via a non-obvious
    # code path (e.g. an in-process ConcurrencyGroup.run_process that inherits
    # os.environ) and forgets to override MNGR_PREFIX would silently create a
    # default-prefixed env that no CI cleanup script recognizes. The earlier
    # guard only catches `mngr_` underscore -- this one also catches dash-
    # prefixed default names like `mngr-<uuid>`.
    if "PYTEST_CURRENT_TEST" in os.environ and not TEST_ENV_PATTERN.match(environment_name):
        raise MngrError(
            f"Refusing to create Modal environment {environment_name!r} during pytest: "
            "test Modal envs must match the mngr_test-YYYY-MM-DD-HH-MM-SS pattern so the "
            "CI cleanup script can find them. Set MNGR_PREFIX via "
            "generate_test_environment_name()."
        )

    with log_span("Creating Modal environment: {}", environment_name):
        try:
            modal_interface.environment_create(environment_name)
            logger.info("Created Modal environment: {}", environment_name)
        except ModalProxyError as e:
            raise MngrError(f"Failed to create Modal environment '{environment_name}': {e}") from e


def _lookup_persistent_app_with_env_retry(
    app_name: str,
    environment_name: str,
    modal_interface: ModalInterface,
    is_environment_creation_allowed: bool,
) -> AppInterface:
    """Look up or create a persistent Modal app, retrying if the environment is not found.

    When ``is_environment_creation_allowed`` is True and the first lookup raises
    ``ModalProxyNotFoundError`` (because the environment does not yet exist), the
    environment is created and the lookup is retried with exponential backoff to
    handle Modal's eventual consistency. When False, the missing-environment
    error is propagated -- callers that are not creating a host should never
    cause a Modal environment to be silently created.
    """
    try:
        return modal_interface.app_lookup(app_name, create_if_missing=True, environment_name=environment_name)
    except ModalProxyNotFoundError:
        if not is_environment_creation_allowed:
            raise
        # Create the environment before retrying
        _create_environment(environment_name, modal_interface)
        return _lookup_persistent_app_with_retry(app_name, environment_name, modal_interface)


@retry(
    retry=retry_if_exception_type(ModalProxyNotFoundError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _lookup_persistent_app_with_retry(
    app_name: str, environment_name: str, modal_interface: ModalInterface
) -> AppInterface:
    """Look up or create a persistent Modal app with tenacity retry."""
    with log_span("Retrying Modal app lookup: {} (env: {})", app_name, environment_name):
        return modal_interface.app_lookup(app_name, create_if_missing=True, environment_name=environment_name)


def _enter_ephemeral_app_context_with_env_retry(
    app: AppInterface,
    environment_name: str,
    modal_interface: ModalInterface,
    is_environment_creation_allowed: bool,
) -> Any:
    """Enter an ephemeral Modal app's run context, retrying if the environment is not found.

    When ``is_environment_creation_allowed`` is True and entering the run context
    raises ``ModalProxyNotFoundError`` (because the environment does not yet exist),
    the environment is created and the entry is retried with exponential backoff to
    handle Modal's eventual consistency. When False, the missing-environment error
    is propagated -- callers that are not creating a host should never cause a
    Modal environment to be silently created.

    Returns the generator context so the caller can manage its lifecycle.
    """
    try:
        gen = app.run(environment_name=environment_name)
        next(gen)
        return gen
    except ModalProxyNotFoundError:
        if not is_environment_creation_allowed:
            raise
        # Create the environment before retrying
        _create_environment(environment_name, modal_interface)
        return _enter_ephemeral_app_context_with_retry(app, environment_name)


@retry(
    retry=retry_if_exception_type(ModalProxyNotFoundError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _enter_ephemeral_app_context_with_retry(app: AppInterface, environment_name: str) -> Any:
    """Enter an ephemeral Modal app's run context with tenacity retry.

    Returns the generator context so the caller can manage its lifecycle.
    """
    with log_span("Retrying Modal app context entry (env: {})", environment_name):
        gen = app.run(environment_name=environment_name)
        next(gen)
        return gen


class ModalAppContextHandle(FrozenModel):
    """Handle for managing a Modal app context lifecycle with output capture.

    This class captures a Modal app's run context along with the output capture
    context. The output buffer can be inspected to detect build failures and
    other issues in the Modal logs.

    Also manages the state volume for persisting host records across sandbox
    termination. The volume is created lazily when first accessed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_context: Any | None = Field(description="The generator from app.run() (only present for ephemeral apps)")
    app_name: str = Field(description="The name of the Modal app")
    environment_name: str = Field(description="The Modal environment name for user isolation")
    output_capture_context: AbstractContextManager[tuple[StringIO, ModalLoguruWriter | None]] = Field(
        description="The output capture context manager"
    )
    output_buffer: StringIO = Field(description="StringIO buffer containing captured Modal output")
    loguru_writer: ModalLoguruWriter | None = Field(description="Loguru writer for structured logging (or None)")
    volume_name: str = Field(description="Name of the state volume for persisting host records")
    volume: VolumeInterface | None = Field(
        default=None, description="The volume interface for state storage (lazily created)"
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
                try:
                    next(handle.run_context)
                except StopIteration:
                    pass
        except ModalProxyError as e:
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
    # Maps app_name -> (AppInterface, ModalAppContextHandle)
    _app_registry: ClassVar[dict[str, tuple[AppInterface, ModalAppContextHandle]]] = {}

    @classmethod
    def _get_or_create_app(
        cls,
        app_name: str,
        environment_name: str,
        is_persistent: bool,
        modal_interface: ModalInterface,
        is_environment_creation_allowed: bool = False,
    ) -> tuple[AppInterface, ModalAppContextHandle]:
        """Get or create a Modal app with output capture.

        Creates an ephemeral app via ``modal_interface.app_create(name)`` and
        enters its ``run()`` context. Apps are cached in the class-level
        registry by name, so repeated calls return the same app. Output
        capture comes from ``modal_interface.enable_output_capture()`` so the
        same body works against any ``ModalInterface`` implementation.

        ``environment_name`` scopes all Modal resources (apps, volumes,
        sandboxes) to a user, isolating between mngr installations sharing
        a Modal account. The state-volume name is prepared here but the
        volume is created lazily by ``get_volume_for_app()``.

        ``is_environment_creation_allowed`` defaults to False: read-only paths
        (list, gc, discover) must not silently create a Modal environment if
        one doesn't exist yet. Only the create-host path sets this to True so
        a brand-new install of mngr can bootstrap the environment on first
        ``mngr create``.

        Raises ``ModalProxyAuthError`` if Modal credentials are missing.
        Raises ``ModalProxyNotFoundError`` if the environment does not exist
        and ``is_environment_creation_allowed`` is False.
        """
        if app_name in cls._app_registry:
            return cls._app_registry[app_name]

        with log_span("Creating ephemeral Modal app with output capture: {} (env: {})", app_name, environment_name):
            with log_span("Enabling Modal output capture"):
                output_capture_context: AbstractContextManager[tuple[StringIO, ModalLoguruWriter | None]] = (
                    modal_interface.enable_output_capture(is_logging_to_loguru=True)
                )
                output_buffer, loguru_writer = output_capture_context.__enter__()

            if is_persistent:
                with log_span("Looking up persistent Modal app: {}", app_name):
                    app = _lookup_persistent_app_with_env_retry(
                        app_name, environment_name, modal_interface, is_environment_creation_allowed
                    )
                run_context = None
            else:
                # Create the Modal app
                with log_span("Creating Modal app object: {}", app_name):
                    app = modal_interface.app_create(app_name)

                # Enter the app.run() context via generator so we can return the app
                # while keeping the context active until close() is called
                with log_span("Entering Modal app.run() context (env: {})", environment_name):
                    run_context = _enter_ephemeral_app_context_with_env_retry(
                        app, environment_name, modal_interface, is_environment_creation_allowed
                    )

            # Set app metadata on the loguru writer for structured logging
            if loguru_writer is not None:
                loguru_writer.app_id = app.get_app_id()
                loguru_writer.app_name = app.get_name()

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
    def get_volume_for_app(cls, app_name: str, modal_interface: ModalInterface) -> VolumeInterface:
        """Get or create the state volume for an app.

        The volume is used to persist host records (including snapshots) across
        sandbox termination. This allows multiple mngr instances to share state
        and enables restoration from snapshots even after the original sandbox
        is gone.

        The volume is created lazily on first access and cached in the context
        handle for subsequent calls. The volume is scoped to the same environment
        as the app.

        Raises MngrError if the app has not been created yet.
        """
        if app_name not in cls._app_registry:
            raise MngrError(f"App {app_name} not found in registry")

        _, context_handle = cls._app_registry[app_name]

        # Return cached volume if already created
        if context_handle.volume is not None:
            return context_handle.volume

        # Create or get the volume in the same environment as the app
        with log_span(
            "Ensuring state volume: {} (env: {})", context_handle.volume_name, context_handle.environment_name
        ):
            volume = modal_interface.volume_from_name(
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
            except ModalProxyError as e:
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
    def _resolve_modal_interface(config: ProviderInstanceConfig) -> ModalInterface:
        """Validate ``config`` is a ``ModalProviderConfig`` and pick the matching ``ModalInterface``.

        Shared by ``build_provider_instance`` and ``bootstrap_for_host_creation``; both call
        sites need the same isinstance guard and the same ``match config.mode`` dispatch.
        Factoring it out keeps the per-mode dispatch in exactly one place so adding a new
        ``ModalMode`` variant only needs editing here.
        """
        if not isinstance(config, ModalProviderConfig):
            raise ConfigStructureError(f"Expected ModalProviderConfig, got {type(config).__name__}")

        match config.mode:
            case ModalMode.DIRECT:
                return DirectModalInterface()
            case ModalMode.PROXIED:
                raise NotImplementedError(
                    "ModalMode.PROXIED (routing through imbue_cloud gateway) is not yet implemented.",
                )
            case _ as unreachable:
                assert_never(unreachable)

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        """Build a Modal provider instance.

        Always treated as a read-or-existing-host construction: if the per-
        user Modal environment does not yet exist, raise ``ProviderEmptyError``
        so read paths (``mngr list`` / ``mngr gc`` / discovery) can skip the
        modal provider entirely rather than silently creating an environment.

        The ``mngr create`` path calls ``bootstrap_for_host_creation`` first,
        which creates the environment if missing; the subsequent
        ``build_provider_instance`` call then succeeds normally.
        """
        modal_interface = ModalProviderBackend._resolve_modal_interface(config)
        return ModalProviderBackend._construct_modal_provider(
            name, config, mngr_ctx, modal_interface, is_environment_creation_allowed=False
        )

    @staticmethod
    def bootstrap_for_host_creation(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> None:
        """Ensure the per-user Modal environment exists; create it if missing.

        Idempotent. Called by ``mngr create`` exactly once per host-create;
        all other call paths build the provider via ``build_provider_instance``
        which never creates an environment.

        Implementation: drive a full ``_construct_modal_provider`` call with
        environment creation allowed. The constructed provider is intentionally
        discarded -- this method's job is the side effect of creating the
        environment (and warming the class-level app registry); the subsequent
        ``build_provider_instance`` call in the create path reuses the cached
        app via ``_get_or_create_app``.
        """
        modal_interface = ModalProviderBackend._resolve_modal_interface(config)
        ModalProviderBackend._construct_modal_provider(
            name, config, mngr_ctx, modal_interface, is_environment_creation_allowed=True
        )

    @staticmethod
    def _construct_modal_provider(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
        modal_interface: ModalInterface,
        is_environment_creation_allowed: bool = False,
    ) -> ProviderInstanceInterface:
        """Build a ``ModalProviderInstance`` against the given ``ModalInterface``.

        Production calls via ``build_provider_instance`` (which passes
        ``is_environment_creation_allowed=False``) or
        ``bootstrap_for_host_creation`` (which passes True). Tests call via
        ``mngr_modal.testing.make_testing_provider`` (which passes a
        ``FakeModalInterface``). Output capture is yielded off
        ``modal_interface.enable_output_capture(...)`` so this function has
        no per-implementation branches.

        ``is_environment_creation_allowed`` gates whether a missing Modal
        environment may be created here. When ``False`` and the environment
        does not exist, ``ProviderEmptyError`` is raised so that read-only
        loaders skip the modal provider entirely rather than creating an
        environment on first use.
        """
        if not isinstance(config, ModalProviderConfig):
            raise ConfigStructureError(f"Expected ModalProviderConfig, got {type(config).__name__}")

        environment_name, app_name, host_dir = ModalProviderBackend._derive_modal_names(name, config, mngr_ctx)

        # Create the ModalProviderApp that manages the Modal app and its resources
        try:
            app, context_handle = ModalProviderBackend._get_or_create_app(
                app_name,
                environment_name,
                config.is_persistent,
                modal_interface,
                is_environment_creation_allowed=is_environment_creation_allowed,
            )
            volume = ModalProviderBackend.get_volume_for_app(app_name, modal_interface)

            modal_app = ModalProviderApp(
                app_name=app_name,
                environment_name=environment_name,
                app=app,
                volume=volume,
                modal_interface=modal_interface,
                close_callback=lambda: ModalProviderBackend.close_app(app_name),
                get_output_callback=lambda: context_handle.output_buffer.getvalue(),
            )
        except ModalProxyAuthError as e:
            # ProviderNotAuthorizedError is a ProviderUnavailableError, so read paths still
            # catch it and keep the provider visible; the dedicated type makes the
            # "not authenticated" case consistent with the other cloud providers.
            raise ProviderNotAuthorizedError(
                name,
                reason="Modal token missing or invalid",
                short_remediation="run `uvx modal token set`",
                user_help_text=(
                    "Modal is not authorized: run `uvx modal token set` to authenticate, or disable this "
                    f"provider with `mngr config set --scope local providers.{name}.is_enabled false`."
                ),
            ) from e
        except ModalProxyNotFoundError as e:
            # Modal environment doesn't exist yet. Only the create-host path is
            # allowed to bootstrap it -- everything else asks the loader to skip
            # the modal provider so commands like `mngr list` / `mngr gc` don't
            # silently create a Modal environment behind the user's back.
            raise ProviderEmptyError(
                provider_name=name,
                reason=(
                    f"Modal environment {environment_name!r} does not exist yet. "
                    f"It will be created the first time you run `mngr create @.{name}`."
                ),
            ) from e
        except ModalProxyError as e:
            raise MngrError(f"Modal provider '{name}' failed to initialize: {e}") from e

        return ModalProviderInstance(
            name=name,
            host_dir=host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            modal_app=modal_app,
        )

    @staticmethod
    def _derive_modal_names(
        name: ProviderInstanceName,
        config: ModalProviderConfig,
        mngr_ctx: MngrContext,
    ) -> tuple[str, str, Path]:
        """Compute the ``(environment_name, app_name, host_dir)`` triple for a Modal provider.

        Pure function (no Modal SDK calls, no filesystem mutation) so the naming
        rules can be unit-tested without instantiating a ModalInterface.

        Conventions:
        - ``environment_name`` = ``f"{prefix}{user_id}"``, truncated to ``MODAL_NAME_MAX_LENGTH``.
          The provider config can override the profile's user_id to allow sharing
          Modal resources across different mngr profiles or installations.
        - ``app_name`` = ``config.app_name`` if set, else ``f"{prefix}{name}"``,
          truncated to leave room for the state-volume suffix.
        - ``host_dir`` = ``config.host_dir`` if set, else ``Path("/mngr")``.

        Logs a warning when truncation actually shortens a name.
        """
        prefix = mngr_ctx.config.prefix
        user_id = config.user_id if config.user_id is not None else mngr_ctx.get_profile_user_id()

        environment_name = f"{prefix}{user_id}"
        if len(environment_name) > MODAL_NAME_MAX_LENGTH:
            logger.warning(
                "Truncating Modal environment name to {} characters: {}", MODAL_NAME_MAX_LENGTH, environment_name
            )
        environment_name = truncate_modal_name(environment_name, max_length=MODAL_NAME_MAX_LENGTH)

        default_app_name = f"{prefix}{name}"
        app_name = config.app_name if config.app_name is not None else default_app_name
        max_app_name_length = MODAL_NAME_MAX_LENGTH - len(STATE_VOLUME_SUFFIX)
        if len(app_name) > max_app_name_length:
            logger.warning("Truncating Modal app name to {} characters: {}", max_app_name_length, app_name)
        app_name = truncate_modal_name(app_name, max_length=max_app_name_length)

        host_dir = config.host_dir if config.host_dir is not None else Path("/mngr")
        return environment_name, app_name, host_dir


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
    mngr_ctx: MngrContext,
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
    return collect_provider_profile_files(mngr_ctx, "modal", _MODAL_EXCLUDED_PROFILE_FILES)


@hookimpl
def on_agent_created(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """We need to snapshot the sandbox after the agents are created and initial messages are delivered."""

    if not isinstance(host, Host):
        raise MngrError("Host is not an instance of Host class")

    provider_instance = host.provider_instance
    if isinstance(provider_instance, ModalProviderInstance):
        provider_instance.on_agent_created(agent, host)
