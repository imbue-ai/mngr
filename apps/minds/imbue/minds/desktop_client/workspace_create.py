"""Shared create-orchestration helpers for the minds desktop client.

These were extracted from ``app.py`` so that both the browser-facing create
routes (in ``app.py``) and the agent-facing ``/api/v1/workspaces`` create
route (in ``api_v1.py``) can build the same backup request, the same
post-create-attempt account-association callback, and resolve/persist the same region.
``api_v1.py`` cannot import ``app.py`` (``app.py`` imports ``api_v1.py``'s
blueprint factory, which would be a cycle), so this lower-level module is the
single home both import.
"""

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import env_text_defines_restic_password
from imbue.minds.desktop_client.imbue_cloud_cli import ActiveShareCache
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.region_preference import GeoLocationCache
from imbue.minds.desktop_client.region_preference import IMBUE_CLOUD_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import VULTR_PROVIDER_KEY
from imbue.minds.desktop_client.region_preference import default_region_for_provider
from imbue.minds.desktop_client.region_preference import known_regions_for_provider
from imbue.minds.desktop_client.region_preference import resolve_default_region
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import enable_web_access_for_workspace
from imbue.minds.desktop_client.state import get_state
from imbue.minds.errors import MindsConfigError
from imbue.minds.errors import WorkspaceSyncError
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import LaunchMode
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

# -- Region resolution --


def region_provider_key_for_launch_mode(launch_mode: LaunchMode) -> str | None:
    """Map a compute launch mode to its region-config provider key, or None if region-less.

    Only ``IMBUE_CLOUD`` and ``VULTR`` offer an ambient region choice at create
    time; ``DOCKER`` / ``LIMA`` run locally, and the cloud modes (AWS / GCP /
    AZURE) are bring-your-own-key-account with placement pinned per account entry.
    """
    if launch_mode is LaunchMode.IMBUE_CLOUD:
        return IMBUE_CLOUD_PROVIDER_KEY
    if launch_mode is LaunchMode.VULTR:
        return VULTR_PROVIDER_KEY
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


# -- Post-create-attempt account-association callback --


class OnCreatedCallback(MutableModel):
    """Callable that records the workspace<->account association.

    ``__call__`` is the single hook that runs once the inner ``mngr create``
    has returned the canonical ``AgentId`` -- before this refactor minds
    pre-generated an id and associated it with the account synchronously
    in the route handler, but for imbue_cloud agents that pre-generated
    id is fictional (the lease forces it back to the pool host's pre-baked
    id), so the association ended up keyed under a phantom row. We now
    do the ``associate_workspace`` call here, where ``agent_id`` is
    guaranteed canonical.

    Sharing is machine-level and user-initiated in the self-hosted relay
    design, so nothing tunnel-related happens at create time anymore.
    """

    session_store: MultiAccountSessionStore = Field(frozen=True, description="Session store for account lookup")
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
            "workspace), in which case no association is recorded."
        ),
    )
    display_name: str = Field(
        frozen=True, default="", description="The user-chosen workspace name, seeded into the workspace record."
    )
    color: str | None = Field(
        frozen=True, default=None, description="The user-chosen accent color, seeded into the workspace record."
    )
    is_cloud_row: bool = Field(
        frozen=True,
        default=False,
        description="True for imbue_cloud compute: the record carries no hosting device (any device may modify).",
    )

    def __call__(self, agent_id: AgentId, host_id: HostId) -> None:
        if not self.account_id:
            return
        # Bind the workspace to the account by seeding its workspace record with
        # the canonical ids. Discovery hasn't seen the workspace yet, so the
        # record starts with just the form metadata; the reconcile's metadata
        # refresh enriches it (provider, secrets) once discovery catches up.
        # Never let an association hiccup fail the create attempt itself.
        try:
            self.session_store.associate_created_workspace(
                user_id=self.account_id,
                agent_id=str(agent_id),
                host_id=str(host_id),
                display_name=self.display_name,
                color=self.color,
                is_cloud_row=self.is_cloud_row,
            )
        except WorkspaceSyncError as exc:
            logger.warning("Could not record the account association for {}: {}", agent_id, exc)
            return
        # Wake the chrome SSE so the workspace tile picks up its new
        # 'account' field immediately. Without this, the chrome shows
        # the workspace as unassociated until the next discovery cycle
        # (~30s+) writes an unrelated change.
        if isinstance(self.backend_resolver, MngrCliBackendResolver):
            self.backend_resolver.notify_change()


class WebAccessEnabler(MutableModel):
    """Post-create hook that brings sharing up so the new workspace is reachable from /web.

    Best-effort by design: it runs after the account association inside the
    agent creator's ``on_created`` (which marks the whole create FAILED on any
    raised exception), and a share bring-up hiccup must not flip an
    already-successful create -- the user can enable sharing from the
    workspace settings instead.
    """

    cli: ImbueCloudCli = Field(frozen=True, description="CLI used for the connector share bring-up")
    session_store: MultiAccountSessionStore = Field(
        frozen=True, description="Session store resolving the workspace's owning account"
    )
    is_cloud_row: bool = Field(
        frozen=True,
        description=(
            "True for imbue_cloud compute: the share bring-up is client-side either way, "
            "but a cloud row skips the desktop-latency relay-region measurement."
        ),
    )
    backend_resolver: BackendResolverInterface = Field(
        frozen=True,
        description="Resolves the workspace shell's origin label, recorded server-side as the chrome's entry origin",
    )
    client_env_config: ClientEnvConfig = Field(
        frozen=True,
        description=(
            "Captured at construction (in the request context) so the connector/broker URLs can be resolved "
            "in the post-create worker thread, where get_state()'s current_app is unbound."
        ),
    )
    active_share_cache: ActiveShareCache = Field(
        frozen=True,
        description=(
            "The readiness poll's connector share-lookup cache (captured in the request context, like "
            "client_env_config). Invalidated after the bring-up attempt, so a negative lookup cached "
            "while this worker was still enabling never delays the ready signal by the cache TTL."
        ),
    )

    def __call__(self, agent_id: AgentId, host_id: HostId) -> None:
        try:
            enable_web_access_for_workspace(
                agent_id=agent_id,
                host_id=str(host_id),
                is_cloud_row=self.is_cloud_row,
                cli=self.cli,
                session_store=self.session_store,
                backend_resolver=self.backend_resolver,
                client_env_config=self.client_env_config,
            )
        except SharingError as exc:
            logger.warning("Could not enable web access for {}: {}", agent_id, exc)
        except Exception as exc:
            # Best-effort side effect: any unexpected failure (a bug, a
            # transport error the share flow did not wrap into SharingError)
            # must be logged with a traceback but never propagate -- this runs
            # in the post-create worker, and a raised error would both crash
            # that worker and skip the create's remaining steps (region
            # persistence). The user can still enable sharing from settings.
            logger.opt(exception=exc).error("Unexpected error enabling web access for {}", agent_id)
        finally:
            # Mirror the sharing PUT handler: the share state may have changed
            # even on failure (the connector create can succeed before the
            # injection fails), and a readiness poll racing this worker may
            # have cached a "not shared" lookup -- drop it so the poll sees
            # the new state immediately rather than at TTL expiry.
            self.active_share_cache.invalidate(str(host_id))


class CreateOnCreatedCallback(MutableModel):
    """Post-create-attempt hook: account association, optional web access, then region persistence.

    Composing these effects into one callable (rather than a nested closure
    at each create call site) keeps the shared create orchestration in one place
    and out of the route handlers.
    """

    base_callback: OnCreatedCallback | None = Field(
        frozen=True,
        default=None,
        description="Account-association callback, or None when no account is selected.",
    )
    minds_config: MindsConfig | None = Field(
        frozen=True, default=None, description="Config used to persist the chosen region as the new default."
    )
    launch_mode: LaunchMode = Field(frozen=True, description="Compute launch mode whose region default is updated.")
    region: str = Field(frozen=True, default="", description="Resolved region to persist on a successful create.")
    web_access_enabler: WebAccessEnabler | None = Field(
        frozen=True,
        default=None,
        description="Brings sharing up post-create when the form's web-access toggle was on.",
    )

    def __call__(self, agent_id: AgentId, host_id: HostId) -> None:
        if self.base_callback is not None:
            self.base_callback(agent_id, host_id)
        # Web access rides on the association above (the share flows resolve
        # the owning account through it), so it runs after.
        if self.web_access_enabler is not None:
            self.web_access_enabler(agent_id, host_id)
        persist_region_for_launch_mode(self.minds_config, self.launch_mode, self.region)


def _build_web_access_enabler(launch_mode: LaunchMode, is_web_access_enabled: bool) -> WebAccessEnabler | None:
    if not is_web_access_enabled:
        return None
    cli = get_state().imbue_cloud_cli
    session_store = get_state().session_store
    client_env_config = get_state().client_env_config
    if cli is None or session_store is None or client_env_config is None:
        # Without the CLI, accounts, or client env config there is nothing to
        # grant (or no connector URL to point the workspace at); the route
        # already refused a web-access create with no account, so this only
        # covers apps assembled without the imbue_cloud integration. Captured
        # here, in the request context, so the enabler -- which runs in a
        # post-create worker thread -- never has to touch current_app.
        logger.warning(
            "Web access was requested but the imbue_cloud CLI, session store, or client env config is not configured"
        )
        return None
    return WebAccessEnabler(
        cli=cli,
        session_store=session_store,
        is_cloud_row=launch_mode is LaunchMode.IMBUE_CLOUD,
        backend_resolver=get_state().backend_resolver,
        client_env_config=client_env_config,
        active_share_cache=get_state().active_share_cache,
    )


def build_create_on_created_callback(
    account_id: str,
    minds_config: MindsConfig | None,
    launch_mode: LaunchMode,
    region: str,
    display_name: str = "",
    color: str | None = None,
    is_web_access_enabled: bool = False,
) -> CreateOnCreatedCallback:
    """Build the composed post-create-attempt callback (association + web access + region persistence)."""
    return CreateOnCreatedCallback(
        base_callback=build_on_created_callback(
            account_id,
            display_name=display_name,
            color=color,
            is_cloud_row=launch_mode is LaunchMode.IMBUE_CLOUD,
        ),
        minds_config=minds_config,
        launch_mode=launch_mode,
        region=region,
        web_access_enabler=_build_web_access_enabler(launch_mode, is_web_access_enabled),
    )


def build_on_created_callback(
    account_id: str,
    display_name: str = "",
    color: str | None = None,
    is_cloud_row: bool = False,
) -> OnCreatedCallback | None:
    """Build a callback that records the account association after an agent create attempt.

    Returns None if no account is selected (nothing to record).
    """
    if not account_id:
        return None

    session_store: MultiAccountSessionStore | None = get_state().session_store
    backend_resolver: BackendResolverInterface = get_state().backend_resolver

    if session_store is None:
        return None

    return OnCreatedCallback(
        session_store=session_store,
        backend_resolver=backend_resolver,
        account_id=account_id,
        display_name=display_name,
        color=color,
        is_cloud_row=is_cloud_row,
    )


# -- Backup request --


def build_backup_request_or_error(
    *,
    backup_provider: BackupProvider,
    api_key_env: str,
    account_email: str,
) -> tuple[BackupSetupRequest | None, str | None]:
    """Resolve form backup inputs into a ``BackupSetupRequest`` or an error message.

    No password is involved: repositories are initialized with each
    workspace's own random password, and the master password's only role in
    the application is wrapping the account's sync DEK (see ``dek_store``).
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
            "Don't set RESTIC_PASSWORD in the backup env -- minds assigns each machine its own random "
            "repository password. Provide RESTIC_REPOSITORY and any backend credentials only."
        )
    return (
        BackupSetupRequest(
            backup_provider=backup_provider,
            api_key_env_text=api_key_env if backup_provider is BackupProvider.API_KEY else "",
            account_email=account_email,
        ),
        None,
    )
