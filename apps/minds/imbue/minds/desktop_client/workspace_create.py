"""Shared create-orchestration helpers for the minds desktop client.

These were extracted from ``app.py`` so that both the browser-facing create
routes (in ``app.py``) and the agent-facing ``/api/v1/workspaces`` create
route (in ``api_v1.py``) can build the same backup request, the same
post-creation tunnel/account callback, and resolve/persist the same region.
``api_v1.py`` cannot import ``app.py`` (``app.py`` imports ``api_v1.py``'s
blueprint factory, which would be a cycle), so this lower-level module is the
single home both import.
"""

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import save_backup_password_if_absent
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import env_text_defines_restic_password
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.region_preference import AWS_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import default_region_for_provider
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.region_preference import resolve_default_region
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.tunnel_token_injection import inject_tunnel_token_into_agent
from imbue.minds.errors import MindsConfigError
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId

# -- Region resolution --


def region_provider_key_for_launch_mode(launch_mode: LaunchMode) -> str | None:
    """Map a compute launch mode to its region-config provider key, or None if region-less.

    Only ``IMBUE_CLOUD``, ``VULTR``, and ``AWS`` place a host in a chosen
    region; ``DOCKER`` / ``LIMA`` run locally and have no region.
    """
    if launch_mode is LaunchMode.IMBUE_CLOUD:
        return IMBUE_CLOUD_PROVIDER_KEY
    if launch_mode is LaunchMode.VULTR:
        return VULTR_PROVIDER_KEY
    if launch_mode is LaunchMode.AWS:
        return AWS_PROVIDER_KEY
    return None


def default_region_for_provider_with_config(
    provider_key: str,
    minds_config: MindsConfig | None,
    geo_cache: GeoLocationCache | None,
) -> str:
    """Resolve the default region to pre-select for a provider (config -> geo -> hardcoded)."""
    configured = minds_config.get_region(provider_key) if minds_config is not None else None
    if geo_cache is not None:
        return resolve_default_region(provider_key, configured, geo_cache)
    # No geo cache (e.g. tests): the stored value if it's a known region, else the hardcoded default.
    if configured and configured in known_regions_for_provider(provider_key):
        return configured
    return default_region_for_provider(provider_key)


def resolve_effective_region(
    launch_mode: LaunchMode,
    submitted_region: str,
    minds_config: MindsConfig | None,
    geo_cache: GeoLocationCache | None,
) -> str:
    """Resolve the region to actually create in for a submitted create request.

    Honors the user's submitted value when it's a known region for the provider;
    otherwise falls back to the same default precedence the form uses. Returns
    "" for region-less providers (DOCKER / LIMA).
    """
    provider_key = region_provider_key_for_launch_mode(launch_mode)
    if provider_key is None:
        return ""
    if submitted_region and submitted_region in known_regions_for_provider(provider_key):
        return submitted_region
    return default_region_for_provider_with_config(provider_key, minds_config, geo_cache)


def persist_region_for_launch_mode(
    minds_config: MindsConfig | None,
    launch_mode: LaunchMode,
    region: str,
) -> None:
    """Persist the chosen region as the provider's new last-used default. Best-effort."""
    provider_key = region_provider_key_for_launch_mode(launch_mode)
    if minds_config is None or provider_key is None or not region:
        return
    # Best-effort: this runs inside the ``on_created`` callback, which the agent
    # creator invokes inside a try/except that marks the create FAILED on any
    # raised exception. A region-persist failure must never flip an
    # already-successful create. ``set_region`` -> ``_write_raw`` can raise a bare
    # ``OSError`` (disk full / permission) in addition to ``MindsConfigError``, so
    # swallow both at debug level.
    try:
        minds_config.set_region(provider_key, region)
    except (MindsConfigError, OSError) as exc:
        logger.debug("Failed to persist region {} for provider {}: {}", region, provider_key, exc)


# -- Post-creation tunnel + account-association callback --


def _run_tunnel_setup(
    agent_id: AgentId,
    imbue_cloud_cli: ImbueCloudCli,
    account_email: str,
    notification_dispatcher: NotificationDispatcher,
    agent_display_name: str,
) -> None:
    """Create a Cloudflare tunnel via the plugin and inject its token into the agent.

    Runs on a detached thread scheduled by ``OnCreatedCallbackFactory`` on
    the desktop client's root ``ConcurrencyGroup``. Failures are logged via
    loguru and surfaced to the user via ``notification_dispatcher``.

    The plugin owns all tunnel state (token, services, auth policy);
    minds keeps no local cache. ``create_tunnel`` is idempotent on the
    connector side, so re-injecting on every agent (re)creation just
    delivers the existing token rather than rotating.
    """
    try:
        info = imbue_cloud_cli.create_tunnel(account=account_email, agent_id=str(agent_id))
    except ImbueCloudCliError as exc:
        logger.warning("Failed to create tunnel for {}: {}", agent_id, exc)
        _notify_tunnel_failure(
            notification_dispatcher=notification_dispatcher,
            agent_display_name=agent_display_name,
            error_message=str(exc),
        )
        return
    if info.token is None:
        logger.warning("Tunnel created for {} but no token returned", agent_id)
        return
    inject_tunnel_token_into_agent(agent_id, info.token.get_secret_value())
    logger.debug("Injected tunnel token into agent {}", agent_id)


def _notify_tunnel_failure(
    notification_dispatcher: NotificationDispatcher,
    agent_display_name: str,
    error_message: str,
) -> None:
    """Dispatch an OS notification for a tunnel-setup failure (no rate limit).

    ``NotificationDispatcher.dispatch`` spawns its own background thread or
    subprocess per channel and swallows channel-specific errors internally,
    so a top-level ``except`` wrapper here would only mask genuine bugs.
    """
    notification_dispatcher.dispatch(
        NotificationRequest(
            title="Tunnel setup failed",
            message=(
                f"Couldn't set up the Cloudflare tunnel for '{agent_display_name}'. "
                f"Sharing may be unavailable. Error: {error_message}"
            ),
            urgency=NotificationUrgency.NORMAL,
        ),
        agent_display_name=agent_display_name,
    )


class OnCreatedCallbackFactory(MutableModel):
    """Callable that records the workspace<->account association and schedules Cloudflare tunnel setup.

    ``__call__`` is the single hook that runs once the inner ``mngr create``
    has returned the canonical ``AgentId`` -- before this refactor minds
    pre-generated an id and associated it with the account synchronously
    in the route handler, but for imbue_cloud agents that pre-generated
    id is fictional (the lease forces it back to the pool host's pre-baked
    id), so the association ended up keyed under a phantom row. We now
    do the ``associate_workspace`` call here, where ``agent_id`` is
    guaranteed canonical.

    The tunnel-setup work is scheduled on a detached thread on the root
    ``ConcurrencyGroup`` so the agent-creation thread can flip status to
    ``DONE`` without waiting on a multi-second Cloudflare round-trip.
    """

    session_store: MultiAccountSessionStore = Field(frozen=True, description="Session store for account lookup")
    imbue_cloud_cli: ImbueCloudCli = Field(
        frozen=True,
        description="CLI wrapper for `mngr imbue_cloud tunnels create`.",
    )
    root_concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description="Root group on which the detached tunnel task is scheduled.",
    )
    notification_dispatcher: NotificationDispatcher = Field(
        frozen=True,
        description="Dispatcher for surfacing tunnel-setup failures as OS notifications.",
    )
    backend_resolver: BackendResolverInterface = Field(
        frozen=True,
        description=(
            "Backend resolver pinged via notify_change() after the association write so the "
            "chrome SSE workspace list refreshes its 'account' field without waiting for the "
            "next 30s discovery heartbeat."
        ),
    )
    account_id: str = Field(
        frozen=True,
        default="",
        description=(
            "Account that owns this workspace. Empty when no account is selected (private "
            "workspace), in which case no association is recorded and no tunnel is set up."
        ),
    )

    def __call__(self, agent_id: AgentId) -> None:
        if not self.account_id:
            return
        # Bind the workspace to the account using the canonical agent id --
        # this is what later ``get_account_for_workspace`` lookups (e.g. for
        # the destruction handler) expect to find.
        self.session_store.associate_workspace(self.account_id, str(agent_id))
        # Wake the chrome SSE so the workspace tile picks up its new
        # 'account' field immediately. Without this, the chrome shows
        # the workspace as unassociated until the next discovery cycle
        # (~30s+) writes an unrelated change.
        if isinstance(self.backend_resolver, MngrCliBackendResolver):
            self.backend_resolver.notify_change()
        account = self.session_store.get_account_for_workspace(str(agent_id))
        if account is None:
            # The account vanished between selection and now (logout?). The
            # association above is still in place; we just skip the tunnel.
            return
        # ``build_on_created_callback`` doesn't have easy access to the
        # user-chosen name at this point (see ``backend_resolver``), so fall
        # back to the short form of the agent id for the notification copy.
        agent_display_name = str(agent_id)[:8]
        self.root_concurrency_group.start_new_thread(
            target=_run_tunnel_setup,
            kwargs={
                "agent_id": agent_id,
                "imbue_cloud_cli": self.imbue_cloud_cli,
                "account_email": str(account.email),
                "notification_dispatcher": self.notification_dispatcher,
                "agent_display_name": agent_display_name,
            },
            name=f"tunnel-setup-{agent_id}",
            # is_checked=False so that a failing tunnel task does not poison
            # the root CG for unrelated strands; failures are surfaced via
            # notifications + loguru from within ``_run_tunnel_setup``.
            is_checked=False,
        )


class CreateOnCreatedCallback(MutableModel):
    """Post-creation hook that runs the tunnel/account callback, then persists the region.

    Composing these two effects into one callable (rather than a nested closure
    at each create call site) keeps the shared create orchestration in one place
    and out of the route handlers.
    """

    base_callback: OnCreatedCallbackFactory | None = Field(
        frozen=True,
        default=None,
        description="Tunnel/account-association callback, or None when no account is selected.",
    )
    minds_config: MindsConfig | None = Field(
        frozen=True, default=None, description="Config used to persist the chosen region as the new default."
    )
    launch_mode: LaunchMode = Field(frozen=True, description="Compute launch mode whose region default is updated.")
    region: str = Field(frozen=True, default="", description="Resolved region to persist on a successful create.")

    def __call__(self, agent_id: AgentId) -> None:
        if self.base_callback is not None:
            self.base_callback(agent_id)
        persist_region_for_launch_mode(self.minds_config, self.launch_mode, self.region)


def build_create_on_created_callback(
    account_id: str,
    minds_config: MindsConfig | None,
    launch_mode: LaunchMode,
    region: str,
) -> CreateOnCreatedCallback:
    """Build the composed post-creation callback (tunnel/account injection + region persistence)."""
    return CreateOnCreatedCallback(
        base_callback=build_on_created_callback(account_id),
        minds_config=minds_config,
        launch_mode=launch_mode,
        region=region,
    )


def build_on_created_callback(
    account_id: str,
) -> OnCreatedCallbackFactory | None:
    """Build a callback that injects the tunnel token after agent creation.

    Returns None if no account is selected (nothing to inject).
    """
    if not account_id:
        return None

    session_store: MultiAccountSessionStore | None = get_state().session_store
    imbue_cloud_cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    root_concurrency_group: ConcurrencyGroup | None = get_state().root_concurrency_group
    notification_dispatcher: NotificationDispatcher | None = get_state().notification_dispatcher
    backend_resolver: BackendResolverInterface = get_state().backend_resolver

    if (
        session_store is None
        or imbue_cloud_cli is None
        or root_concurrency_group is None
        or notification_dispatcher is None
    ):
        return None

    return OnCreatedCallbackFactory(
        session_store=session_store,
        imbue_cloud_cli=imbue_cloud_cli,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        backend_resolver=backend_resolver,
        account_id=account_id,
    )


# -- Backup request --


def build_backup_request_or_error(
    *,
    backup_provider: BackupProvider,
    encryption_method: BackupEncryptionMethod,
    typed_master_password: str,
    is_save_password: bool,
    api_key_env: str,
    account_email: str,
    paths: WorkspacePaths,
) -> tuple[BackupSetupRequest | None, str | None]:
    """Resolve form backup inputs into a ``BackupSetupRequest`` or an error message.

    Reads / first-time-saves the shared master password as a side effect.
    Returns ``(request, None)`` on success or ``(None, message)`` for a
    validation error the caller should re-render on the form.
    """
    if backup_provider is BackupProvider.CONFIGURE_LATER:
        return BackupSetupRequest(backup_provider=BackupProvider.CONFIGURE_LATER), None
    if backup_provider is BackupProvider.IMBUE_CLOUD and not account_email:
        return None, (
            "imbue_cloud backups require a selected account. Choose an account or pick a different backup provider."
        )
    # The user never sets the repository password: minds initializes the repo
    # and assigns each workspace its own random RESTIC_PASSWORD, so reject it
    # if a user puts one in the api_key env block.
    if backup_provider is BackupProvider.API_KEY and env_text_defines_restic_password(api_key_env):
        return None, (
            "Don't set RESTIC_PASSWORD in the backup env -- minds assigns each workspace its own random "
            "repository password. Provide RESTIC_REPOSITORY and any backend credentials only."
        )
    # The master password (or empty, for no_password) is used only to
    # initialize the repo from the minds machine; it never enters the workspace.
    master_password: SecretStr | None = None
    if encryption_method is BackupEncryptionMethod.MASTER_PASSWORD:
        saved_password = read_saved_backup_password(paths)
        if saved_password is not None:
            master_password = SecretStr(saved_password)
        elif typed_master_password:
            master_password = SecretStr(typed_master_password)
            if is_save_password:
                save_backup_password_if_absent(paths, typed_master_password)
        else:
            return None, "Enter a backup master password, or set the encryption method to 'no password'."
    return (
        BackupSetupRequest(
            backup_provider=backup_provider,
            master_password=master_password,
            api_key_env_text=api_key_env if backup_provider is BackupProvider.API_KEY else "",
            account_email=account_email,
        ),
        None,
    )
