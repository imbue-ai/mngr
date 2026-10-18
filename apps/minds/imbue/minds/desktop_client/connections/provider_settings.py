"""The materializer: the single writer of every account and connection provider block.

The pre-import reconcile (``mngr_settings/reconcile.py``) owns only what must
be on disk before mngr loads: the plugin block, the top-level settings, and
the default-instance suppression blocks. The materializer re-runs it on every
pass, because the startup call no-ops on a fresh env whose mngr profile dir
does not exist yet (mngr creates it moments later), and the first signin must
still land the suppression blocks. Everything else under ``[providers.*]``
that minds manages -- one ``imbue_cloud_<user id>`` block per signed-in
account and one ``<backend>_<hex>`` block per connection whose credentials
are available here -- is derived by :func:`desired_provider_blocks` and
written whole by :func:`reconcile_provider_settings`, which also deletes stale
managed blocks. Every write goes through :class:`ProviderSettingsMaterializer`,
which bounces discovery when the file changed.
"""

import json
import threading
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import AbstractSet
from typing import Final
from typing import assert_never

import tomlkit
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.connection_store import is_connection_metadata_too_new
from imbue.minds.desktop_client.connections.connection_store import parse_connection_metadata
from imbue.minds.desktop_client.connections.data_types import AwsPlacement
from imbue.minds.desktop_client.connections.data_types import AzurePlacement
from imbue.minds.desktop_client.connections.data_types import CONNECTION_STATE_ACTIVE
from imbue.minds.desktop_client.connections.data_types import ConnectionCredentials
from imbue.minds.desktop_client.connections.data_types import ConnectionMetadata
from imbue.minds.desktop_client.connections.data_types import ConnectionReplicaRecord
from imbue.minds.desktop_client.connections.data_types import GcpPlacement
from imbue.minds.desktop_client.connections.data_types import ModalPlacement
from imbue.minds.desktop_client.connections.data_types import ProviderMaterializationOutcome
from imbue.minds.desktop_client.connections.data_types import SignedInAccount
from imbue.minds.desktop_client.connections.data_types import parse_connection_backend
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import ConnectionId
from imbue.minds.desktop_client.connections.primitives import IMBUE_CLOUD_PROVIDER_NAME_PREFIX
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import MODAL_SANDBOX_TIMEOUT_SECONDS_MAX
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import is_connection_instance_name
from imbue.minds.desktop_client.connections.primitives import is_derived_provider_instance_name
from imbue.minds.desktop_client.connections.primitives import mngr_backend_name
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.errors import AccountsIndexUnreadableError
from imbue.minds.mngr_settings.errors import MindsSettingsError
from imbue.minds.mngr_settings.file_store import settings_store_for
from imbue.minds.mngr_settings.profile_paths import read_active_profile_dir
from imbue.minds.mngr_settings.provider_blocks import CLOUD_CONNECTION_DOCKER_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import CLOUD_CONNECTION_INSTALL_GVISOR_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_DEFAULT_START_ARGS
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_DOCKER_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import LEGACY_BYOK_PROVIDER_NAME_PREFIX
from imbue.minds.mngr_settings.provider_blocks import MODAL_MODE_DIRECT
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.primitives import slugify_account

# Modal sizing and sandbox lifetime for minds workspaces: 2 CPU / 4 GB (the
# lima/docker convention) and the longest sandbox Modal allows (~24h); idle
# detection is disabled by the create template, so the idle timeout only has
# to stay above the sandbox lifetime.
MODAL_DEFAULT_CPU: Final[float] = 2.0
MODAL_DEFAULT_MEMORY_GB: Final[float] = 4.0
MODAL_DEFAULT_SANDBOX_TIMEOUT_SECONDS: Final[int] = MODAL_SANDBOX_TIMEOUT_SECONDS_MAX
MODAL_DEFAULT_IDLE_TIMEOUT_SECONDS: Final[int] = 86400


@pure
def _cloud_connection_hardening_knobs() -> dict[str, object]:
    """Every cloud VM runs the agent under gVisor: the host setup installs the runtime and mngr_vps adds the tmpfs mounts runsc needs."""
    return {
        "install_gvisor_runtime": CLOUD_CONNECTION_INSTALL_GVISOR_RUNTIME,
        "docker_runtime": CLOUD_CONNECTION_DOCKER_RUNTIME,
    }


@pure
def _user_data_layout_knobs() -> dict[str, object]:
    return {
        "host_dir": WORKSPACE_HOST_DIR,
        "volume_home_path": WORKSPACE_VOLUME_HOME_PATH,
        "host_log_dir": WORKSPACE_HOST_LOG_DIR,
    }


@pure
def account_provider_block(email: str, connector_url: str) -> dict[str, object]:
    """The ``[providers.imbue_cloud_<user id>]`` block for one signed-in account."""
    return {
        "backend": IMBUE_CLOUD_BACKEND_NAME,
        "account": email,
        "connector_url": connector_url,
        "docker_runtime": IMBUE_CLOUD_DOCKER_RUNTIME,
        "install_gvisor_runtime": IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME,
        "default_start_args": list(IMBUE_CLOUD_DEFAULT_START_ARGS),
        **_user_data_layout_knobs(),
    }


def _secret(value: SecretStr) -> str:
    """The plaintext of a secret credentials field, as the settings file must carry it."""
    return value.get_secret_value()


@pure
def connection_provider_block(
    backend: ConnectionBackend,
    connection_id: ConnectionId,
    metadata: ConnectionMetadata,
    credentials: ConnectionCredentials,
) -> dict[str, object] | None:
    """The ``[providers.<backend>_<hex>]`` block for one connection.

    Returns None when the credentials lack a field the backend needs; raises
    ``ValueError`` when the placement metadata is malformed.
    """
    match backend:
        case ConnectionBackend.AWS:
            aws_placement = AwsPlacement.model_validate(metadata.placement)
            if credentials.aws_access_key_id is None or credentials.aws_secret_access_key is None:
                return None
            return {
                "backend": mngr_backend_name(backend),
                "default_region": aws_placement.default_region,
                "default_instance_type": aws_placement.default_instance_type,
                **_cloud_connection_hardening_knobs(),
                **_user_data_layout_knobs(),
                "aws_access_key_id": credentials.aws_access_key_id,
                "aws_secret_access_key": _secret(credentials.aws_secret_access_key),
            }
        case ConnectionBackend.GCP:
            gcp_placement = GcpPlacement.model_validate(metadata.placement)
            if credentials.service_account_key_json is None:
                return None
            return {
                "backend": mngr_backend_name(backend),
                "default_zone": gcp_placement.default_zone,
                "default_machine_type": gcp_placement.default_machine_type,
                **_cloud_connection_hardening_knobs(),
                **_user_data_layout_knobs(),
                "service_account_key_json": _secret(credentials.service_account_key_json),
            }
        case ConnectionBackend.AZURE:
            azure_placement = AzurePlacement.model_validate(metadata.placement)
            if (
                credentials.subscription_id is None
                or credentials.tenant_id is None
                or credentials.client_id is None
                or credentials.client_secret is None
            ):
                return None
            return {
                "backend": mngr_backend_name(backend),
                "default_region": azure_placement.default_region,
                "default_vm_size": azure_placement.default_vm_size,
                "resource_group": azure_placement.resource_group,
                **_cloud_connection_hardening_knobs(),
                **_user_data_layout_knobs(),
                "subscription_id": credentials.subscription_id,
                "tenant_id": credentials.tenant_id,
                "client_id": credentials.client_id,
                "client_secret": _secret(credentials.client_secret),
            }
        case ConnectionBackend.MODAL:
            modal_placement = ModalPlacement.model_validate(metadata.placement)
            if credentials.token_id is None or credentials.token_secret is None:
                return None
            # ``user_id`` names the Modal environment (see ModalProviderBackend._derive_modal_names):
            # the connection id hex, so every device derives the same one, unless a
            # this-device-only connection named its own at registration.
            return {
                "backend": mngr_backend_name(backend),
                "mode": MODAL_MODE_DIRECT,
                "is_persistent": True,
                "user_id": modal_placement.user_id or connection_id.get_uuid().hex,
                "default_cpu": MODAL_DEFAULT_CPU,
                "default_memory": MODAL_DEFAULT_MEMORY_GB,
                "default_sandbox_timeout": MODAL_DEFAULT_SANDBOX_TIMEOUT_SECONDS,
                "default_idle_timeout": MODAL_DEFAULT_IDLE_TIMEOUT_SECONDS,
                "token_id": credentials.token_id,
                "token_secret": _secret(credentials.token_secret),
            }
        case _ as unreachable:
            assert_never(unreachable)


def desired_provider_blocks(
    accounts: Sequence[SignedInAccount],
    connections_by_owner: Mapping[str, Sequence[ConnectionReplicaRecord]],
    credentials_by_connection_id: Mapping[str, ConnectionCredentials],
    connector_url: str,
) -> dict[str, dict[str, object]]:
    """The complete set of managed instance blocks, keyed by provider instance name.

    One account instance per signed-in account, and one connection instance
    per ACTIVE connection whose credentials are available (present in
    ``credentials_by_connection_id``) and whose formats this app supports. A
    locked account contributes its account instance but none of its
    connections; a connection with an unknown backend or malformed placement
    is skipped with a warning (shown as read-only elsewhere, never dropped).
    """
    blocks: dict[str, dict[str, object]] = {}
    for account in accounts:
        blocks[imbue_cloud_provider_name_for_user(account.user_id)] = account_provider_block(
            account.email, connector_url
        )
    for owner_user_id, records in connections_by_owner.items():
        for record in records:
            named_block = _connection_instance_block(owner_user_id, record, credentials_by_connection_id)
            if named_block is not None:
                instance_name, block = named_block
                blocks[instance_name] = block
    return blocks


def _connection_instance_block(
    owner_user_id: str,
    record: ConnectionReplicaRecord,
    credentials_by_connection_id: Mapping[str, ConnectionCredentials],
) -> tuple[str, dict[str, object]] | None:
    """``(instance name, block)`` for one connection record, or None when it is not materialized here."""
    if record.state != CONNECTION_STATE_ACTIVE or is_connection_metadata_too_new(record):
        return None
    credentials = credentials_by_connection_id.get(record.doc_id)
    if credentials is None:
        return None
    metadata = parse_connection_metadata(record)
    if metadata is None:
        return None
    backend = parse_connection_backend(metadata)
    if backend is None:
        logger.warning(
            "Connection {} names an unknown backend {!r}; not materialized", record.doc_id, metadata.backend
        )
        return None
    try:
        connection_id = ConnectionId(record.doc_id)
    except ValueError:
        logger.warning("Connection document id {!r} is not a connection id; not materialized", record.doc_id)
        return None
    try:
        block = connection_provider_block(backend, connection_id, metadata, credentials)
    except ValueError as e:
        logger.warning("Connection {} of owner {} has malformed placement: {}", record.doc_id, owner_user_id[:8], e)
        return None
    if block is None:
        logger.warning("Connection {} lacks the credential fields its backend needs; not materialized", record.doc_id)
        return None
    return provider_instance_name_for_connection(backend, connection_id), block


@pure
def is_managed_provider_name(provider_name: str) -> bool:
    """Whether a ``[providers.<name>]`` block is one the materializer owns (and may delete)."""
    return (
        provider_name.startswith(IMBUE_CLOUD_PROVIDER_NAME_PREFIX)
        or is_connection_instance_name(provider_name)
        or provider_name.startswith(LEGACY_BYOK_PROVIDER_NAME_PREFIX)
    )


def is_provider_polled_here(root: MindsRoot, provider_name: str) -> bool:
    """Whether discovery on this device polls the named provider instance right now.

    An instance is polled when the active mngr profile's settings hold an
    enabled ``[providers.<provider_name>]`` block for it, or when it has no
    block and is not a name the materializer derives: a backend's own default
    instance (``local``, ``docker``, ``lima``, ...) is polled without a block,
    while an account or connection instance exists only through its block. A
    suppressed default (``is_enabled = false``, which the pre-import reconcile
    pins on the five remote backends) is not polled.

    Callers drop discovery for an instance this says is not polled, so a
    settings file that cannot be read or parsed answers True after a warning:
    it proves nothing about the instance. No profile or no file keeps the
    backend defaults alone.
    """
    store = settings_store_for(root)
    if store is None:
        return not is_derived_provider_instance_name(provider_name)
    try:
        document = store.read()
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(
            "Could not read the mngr settings at {}; treating provider {} as still polled: {}",
            store.settings_path,
            provider_name,
            e,
        )
        return True
    providers = document.get("providers", {})
    block = providers.get(provider_name) if isinstance(providers, dict) else None
    if isinstance(block, dict):
        return block.get("is_enabled") is not False
    return not is_derived_provider_instance_name(provider_name)


class ProviderReconcileResult(FrozenModel):
    """What one reconcile of the managed provider blocks did to the settings file."""

    is_changed: bool = Field(description="Whether the file was rewritten")
    removed_instance_names: tuple[str, ...] = Field(
        description="Managed blocks deleted because they are no longer desired (their hosts are gone from this device)"
    )


def _reconcile_document(
    doc: tomlkit.TOMLDocument, desired: Mapping[str, Mapping[str, object]], removed_instance_names_out: list[str]
) -> bool:
    providers = doc.get("providers")
    # A dict check, not a Table one: tomlkit hands back an OutOfOrderTableProxy
    # when the [providers.*] tables are split around another section.
    if not isinstance(providers, dict):
        providers = tomlkit.table()
        doc["providers"] = providers
    is_changed = False
    for name in list(providers.keys()):
        if is_managed_provider_name(str(name)) and str(name) not in desired:
            if str(name).startswith(LEGACY_BYOK_PROVIDER_NAME_PREFIX):
                logger.warning(
                    "Removing the legacy bring-your-own-key provider block {}: cloud accounts are now connections "
                    "owned by an imbue cloud account (or the local profile); re-add it from the providers panel",
                    name,
                )
            del providers[name]
            removed_instance_names_out.append(str(name))
            is_changed = True
    for name, block in desired.items():
        existing = providers.get(name)
        if isinstance(existing, dict) and existing.unwrap() == dict(block):
            continue
        new_block = tomlkit.table()
        for key, value in block.items():
            new_block[key] = value
        providers[name] = new_block
        is_changed = True
    return is_changed


def reconcile_provider_settings(
    root: MindsRoot, desired: Mapping[str, Mapping[str, object]]
) -> ProviderReconcileResult:
    """Write the desired managed blocks whole and delete stale ones; reports whether the file changed and what went.

    Runs under the settings store's write lock. Skips (an unchanged result) while
    mngr is uninitialized in this host dir, since there is nothing to write to.
    """
    store = settings_store_for(root)
    if store is None:
        return ProviderReconcileResult(is_changed=False, removed_instance_names=())
    removed_instance_names: list[str] = []
    is_modified = store.update(
        lambda doc: _reconcile_document(doc, desired, removed_instance_names_out=removed_instance_names)
    )
    if is_modified:
        logger.debug("Updated the managed provider blocks in {}", store.settings_path)
    return ProviderReconcileResult(is_changed=is_modified, removed_instance_names=tuple(removed_instance_names))


# CLEANUP: drop migrate_imbue_cloud_state_dirs (and its call in
# ProviderSettingsMaterializer._materialize_from) once no install can still hold an
# imbue_cloud_<email-slug> state dir, i.e. a release or two after the first one
# that names account instances by user id.
def legacy_imbue_cloud_provider_name_for_account(email: str) -> str:
    """The email-slug instance name older minds builds wrote (``imbue_cloud_<email-slug>``).

    Kept for the one-time state-dir move and because synced records and the connector's lease
    stub still carry this spelling on the wire; every reader accepts both spellings.
    """
    return f"imbue_cloud_{slugify_account(email)}"


def migrate_imbue_cloud_state_dirs(root: MindsRoot, accounts: Sequence[SignedInAccount]) -> None:
    """Rename each signed-in account's ``imbue_cloud_<email-slug>`` state dir to ``imbue_cloud_<user id>``.

    Carries the per-host client keys, known_hosts, and lease caches across the
    naming change. When both directories exist the user-id one is kept and the
    legacy one is left in place with a warning. Accounts that are not signed in
    keep their legacy directory until they sign in again.
    """
    profile_dir = read_active_profile_dir(root.mngr_host_dir)
    if profile_dir is None:
        return
    state_root = profile_dir / "providers" / IMBUE_CLOUD_BACKEND_NAME
    for account in accounts:
        try:
            legacy_name = legacy_imbue_cloud_provider_name_for_account(account.email)
        except MindsSettingsError as e:
            logger.warning("Skipping the imbue_cloud state-dir migration for {!r}: {}", account.email, e)
            continue
        legacy_dir = state_root / legacy_name
        target_dir = state_root / imbue_cloud_provider_name_for_user(account.user_id)
        if not legacy_dir.is_dir():
            continue
        if target_dir.exists():
            logger.warning(
                "Both {} and {} exist; keeping the user-id state dir and leaving the legacy one in place",
                target_dir,
                legacy_dir,
            )
            continue
        try:
            legacy_dir.rename(target_dir)
        except OSError as e:
            logger.warning("Could not rename the imbue_cloud state dir {} to {}: {}", legacy_dir, target_dir, e)
            continue
        logger.info("Moved the imbue_cloud state dir for account {} to {}", account.user_id[:8], target_dir.name)


def imbue_cloud_accounts_index_path(root: MindsRoot) -> Path | None:
    """The plugin's ``accounts.json`` session index for the active profile, or None while no profile is set."""
    profile_dir = read_active_profile_dir(root.mngr_host_dir)
    if profile_dir is None:
        return None
    return get_sessions_dir(profile_dir) / "accounts.json"


def read_signed_in_accounts_from_index(root: MindsRoot) -> list[SignedInAccount]:
    """The signed-in accounts per the plugin's ``accounts.json`` index (no subprocess).

    The startup materializer reads this file so the first ``mngr observe``
    sees the derived provider set without waiting for a warm mngr process;
    every later trigger lists accounts through the session store instead.
    Entries without both a user id and an email are skipped with a warning.
    An absent index is an empty account list; an index that exists but cannot
    be read as one raises ``AccountsIndexUnreadableError``, since a failed
    listing must not be materialized as "no accounts".
    """
    accounts_path = imbue_cloud_accounts_index_path(root)
    if accounts_path is None or not accounts_path.is_file():
        return []
    try:
        index_text = accounts_path.read_text()
    except OSError as e:
        raise AccountsIndexUnreadableError(
            f"Could not read the imbue_cloud accounts index {accounts_path}: {e}"
        ) from e
    try:
        raw = json.loads(index_text)
    except json.JSONDecodeError as e:
        raise AccountsIndexUnreadableError(f"Malformed imbue_cloud accounts index {accounts_path}: {e}") from e
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise AccountsIndexUnreadableError(f"The imbue_cloud accounts index {accounts_path} has no entries list")
    accounts: list[SignedInAccount] = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("Skipping an imbue_cloud accounts index entry that is not an object: {!r}", entry)
            continue
        user_id = entry.get("user_id")
        email = entry.get("email")
        if not isinstance(user_id, str) or not user_id or not isinstance(email, str) or not email:
            logger.warning("Skipping an imbue_cloud accounts index entry without a user id and email: {}", entry)
            continue
        accounts.append(SignedInAccount(user_id=user_id, email=email))
    return accounts


def read_signed_in_accounts_from_index_or_none(root: MindsRoot) -> list[SignedInAccount] | None:
    """The startup materializer's account source: the index, or None (with a warning) when it cannot be read."""
    try:
        return read_signed_in_accounts_from_index(root)
    except AccountsIndexUnreadableError as e:
        logger.warning("Could not read the imbue_cloud accounts index at startup: {}", e)
        return None


def desired_provider_blocks_from_store(
    accounts: Sequence[SignedInAccount],
    connection_store: ConnectionStore,
    connector_url: str,
) -> dict[str, dict[str, object]]:
    """Assemble the materializer inputs from the replicas and the DEKs on this device."""
    signed_in_user_ids = [account.user_id for account in accounts]
    connections_by_owner: dict[str, list[ConnectionReplicaRecord]] = {
        user_id: connection_store.list_active_connections(user_id)
        for user_id in [*signed_in_user_ids, LOCAL_PROFILE_USER_ID]
    }
    return desired_provider_blocks(
        accounts,
        connections_by_owner,
        connection_store.credentials_by_connection_id(signed_in_user_ids),
        connector_url,
    )


class ProviderSettingsMaterializer(MutableModel):
    """The single writer of managed provider blocks; bounces discovery when the file changed.

    The account list is resolved under the materializer's own lock, by the
    ``list_accounts`` source, at the moment the blocks are derived: a caller
    that snapshotted the accounts earlier (a sync pass that spent seconds in
    network pulls) could otherwise write back an account that signed out in
    between.
    """

    root_name: str = Field(frozen=True, description="The active minds env's MINDS_ROOT_NAME")
    connection_store: ConnectionStore = Field(frozen=True, description="Source of connections and credentials")
    connector_url: str = Field(frozen=True, description="Connector URL written into every account block")
    list_accounts: Callable[[], Sequence[SignedInAccount] | None] = Field(
        frozen=True,
        description="Lists the signed-in accounts the provider set is derived from; None when the listing failed",
    )
    on_settings_changed: Callable[[], None] | None = Field(
        default=None,
        frozen=True,
        description="Invoked after a write that changed the file (the observe bounce); None in tests",
    )
    on_provider_instances_removed: Callable[[AbstractSet[str]], None] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Invoked with the managed instance names a write deleted (a removed or re-homed connection, a signed-out "
            "account), so discovery state for their hosts is dropped rather than kept as last-good; None in tests"
        ),
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def materialize(self) -> ProviderMaterializationOutcome:
        """Derive and write the provider set for the accounts ``list_accounts`` reports; bounces discovery on a change."""
        outcome = self._materialize_from(self.list_accounts)
        if outcome is ProviderMaterializationOutcome.CHANGED and self.on_settings_changed is not None:
            self.on_settings_changed()
        return outcome

    def _report_removed_instances(self, removed_instance_names: tuple[str, ...]) -> None:
        if removed_instance_names and self.on_provider_instances_removed is not None:
            self.on_provider_instances_removed(frozenset(removed_instance_names))

    def materialize_without_bounce(
        self, list_accounts_at_startup: Callable[[], Sequence[SignedInAccount] | None]
    ) -> ProviderMaterializationOutcome:
        """The startup write, before any observer has been spawned: no bounce, and its own account source.

        Startup reads the plugin's session index directly (``read_signed_in_accounts_from_index``)
        rather than through the session store, which would wait on the warm mngr process
        loading before the first observer could even be spawned.
        """
        return self._materialize_from(list_accounts_at_startup)

    def _materialize_from(
        self, list_accounts: Callable[[], Sequence[SignedInAccount] | None]
    ) -> ProviderMaterializationOutcome:
        root = MindsRoot(self.root_name)
        with self._lock:
            accounts = list_accounts()
            if accounts is None:
                # A failed listing is not an empty one: writing it would delete every
                # account and connection block, so the next trigger writes them.
                logger.warning("Leaving the provider settings as they are: the account listing failed")
                return ProviderMaterializationOutcome.SKIPPED
            is_pre_import_shape_changed = ensure_mngr_settings(root)
            migrate_imbue_cloud_state_dirs(root, accounts)
            desired = desired_provider_blocks_from_store(
                accounts,
                self.connection_store,
                self.connector_url,
            )
            reconcile_result = reconcile_provider_settings(root, desired)
        self._report_removed_instances(reconcile_result.removed_instance_names)
        if is_pre_import_shape_changed or reconcile_result.is_changed:
            return ProviderMaterializationOutcome.CHANGED
        return ProviderMaterializationOutcome.UNCHANGED
