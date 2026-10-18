import os
import sys
from typing import Final

import tomlkit
from loguru import logger
from tomlkit.items import Table

from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings._migrations import apply_data_dir_migrations
from imbue.minds.mngr_settings._migrations import apply_one_shot_settings_migrations
from imbue.minds.mngr_settings._migrations import apply_settings_document_migrations
from imbue.minds.mngr_settings.errors import MindsSettingsError
from imbue.minds.mngr_settings.file_store import settings_store_for
from imbue.minds.mngr_settings.provider_blocks import AWS_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import AZURE_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import GCP_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import MODAL_BACKEND_NAME


def ensure_mngr_settings_before_mngr_import() -> None:
    """Reconcile the mngr settings for the active env, before mngr is imported.

    Called from ``main.py`` between ``apply_bootstrap()`` and the ``cli_entry`` import.
    mngr consults settings.toml during its own import-time initialization (plugin blocking, config discovery), so the Imbue Studio overrides must be on disk before any ``imbue.mngr.*`` module loads -- this function enforces that ordering at runtime.
    No-op when ``MINDS_ROOT_NAME`` is unset (an unactivated shell has nothing to reconcile).
    """
    already_imported = [name for name in sys.modules if name == "imbue.mngr" or name.startswith("imbue.mngr.")]
    if already_imported:
        raise MindsSettingsError(
            f"mngr settings must be reconciled before importing mngr, but these modules are already "
            f"loaded: {already_imported[:5]}"
        )
    if os.environ.get(MINDS_ROOT_NAME_ENV_VAR) is None:
        return
    ensure_mngr_settings(MindsRoot.from_environment())


# The default-instance suppression blocks, keyed by provider name, each pinned
# to ``is_enabled = false`` on every reconcile: mngr auto-creates a default
# provider instance for every registered backend, and those credential-less
# defaults fail every ``mngr list`` discovery cycle with spurious warnings. The
# usable instances are derived by the materializer in
# ``desktop_client/connections/provider_settings.py`` after mngr is importable;
# this pre-import reconcile owns only what must be on disk before ``imbue.mngr``
# loads.
_DESIRED_PROVIDER_BLOCKS: Final[tuple[tuple[str, dict[str, object]], ...]] = (
    (IMBUE_CLOUD_BACKEND_NAME, {"backend": IMBUE_CLOUD_BACKEND_NAME, "is_enabled": False}),
    (AWS_BACKEND_NAME, {"backend": AWS_BACKEND_NAME, "is_enabled": False}),
    (GCP_BACKEND_NAME, {"backend": GCP_BACKEND_NAME, "is_enabled": False}),
    (AZURE_BACKEND_NAME, {"backend": AZURE_BACKEND_NAME, "is_enabled": False}),
    (MODAL_BACKEND_NAME, {"backend": MODAL_BACKEND_NAME, "is_enabled": False}),
)

# Plugins Imbue Studio disables for every mngr subprocess it spawns, keyed by the
# pluggy entry-point name (NOT the package name -- mngr matches plugin-blocking
# section names verbatim against the registered name).
#
# ``recursive`` is disabled because its on_host_created hook injects the
# calling user's local ~/.claude/ and ~/.mngr/ deploy files into the workspace,
# contradicting the contract that the repo is the full definition of the
# workspace. Imbue Studio runs inside its own MNGR_HOST_DIR profile, so this only
# affects the subprocesses Imbue Studio spawns.
_DESIRED_PLUGIN_BLOCKS: Final[tuple[tuple[str, dict[str, object]], ...]] = (("recursive", {"enabled": False}),)

# Top-level settings Imbue Studio pins for every profile it manages.
#
# ``default_destroyed_host_persisted_seconds`` keeps destroyed mngr host
# records for 30 days (mngr's own default is 7), matching the
# destroyed-workspace backup retention window so host records and backups age
# out together. Only the profiles Imbue Studio manages change.
_DESTROYED_HOST_PERSISTED_SECONDS: Final[int] = 60 * 60 * 24 * 30
_DESIRED_TOP_LEVEL_SETTINGS: Final[dict[str, object]] = {
    "default_destroyed_host_persisted_seconds": _DESTROYED_HOST_PERSISTED_SECONDS,
}


def _merge_block(section: Table | tomlkit.TOMLDocument, name: str, pinned: dict[str, object]) -> bool:
    """Merge one desired block into the section, returning whether anything changed.

    Pinned fields are forced to their desired values; fields minds does not manage are never touched.
    """
    block = section.get(name)
    if not isinstance(block, dict):
        block = tomlkit.table()
        section[name] = block
    is_changed = False
    for key, desired_value in pinned.items():
        if block.get(key) != desired_value:
            block[key] = desired_value
            is_changed = True
    return is_changed


def _reconcile_document(doc: tomlkit.TOMLDocument) -> bool:
    """Bring the settings document to the shape Imbue Studio wants; returns whether it changed."""
    is_changed = apply_settings_document_migrations(doc)
    providers_section = doc.setdefault("providers", tomlkit.table())

    for name, pinned in _DESIRED_PROVIDER_BLOCKS:
        is_changed = _merge_block(providers_section, name, pinned) or is_changed

    plugins_section = doc.setdefault("plugins", tomlkit.table())
    for name, pinned in _DESIRED_PLUGIN_BLOCKS:
        is_changed = _merge_block(plugins_section, name, pinned) or is_changed

    for key, desired_value in _DESIRED_TOP_LEVEL_SETTINGS.items():
        if doc.get(key) != desired_value:
            doc[key] = desired_value
            is_changed = True
    return is_changed


def ensure_mngr_settings(root: MindsRoot) -> bool:
    """Ensure the mngr settings.toml carries the Imbue Studio overrides.

    Reconciles the file to the desired shape declared above, writing only when something actually drifted.
    Returns ``True`` when the file was written -- the provider set visible to a running ``mngr observe`` changed, so callers that hold a supervisor handle should bounce the observe child.
    Skips silently when mngr hasn't been initialized in this host_dir yet (no ``config.toml`` / no profile dir) -- there's nothing to write to.
    """
    store = settings_store_for(root)
    if store is None:
        return False
    is_modified = store.update(_reconcile_document)
    if is_modified:
        logger.debug("Updated mngr settings at {} with Imbue Studio overrides", store.settings_path)
    is_migrated = apply_one_shot_settings_migrations(root, store)
    apply_data_dir_migrations(root)
    return is_modified or is_migrated
