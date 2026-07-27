import json
import os
from collections.abc import Mapping
from typing import Final

import tomlkit
from loguru import logger

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.data_types import CloudAccountRecord
from imbue.minds.mngr_settings.errors import MindsSettingsError
from imbue.minds.mngr_settings.file_store import settings_store_for
from imbue.minds.mngr_settings.provider_blocks import AWS_DEFAULT_INSTANCE_TYPE
from imbue.minds.mngr_settings.provider_blocks import AWS_DEFAULT_START_ARGS
from imbue.minds.mngr_settings.provider_blocks import AWS_DOCKER_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import AWS_INSTALL_GVISOR_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import BYOK_PROVIDER_NAME_PREFIX
from imbue.minds.mngr_settings.provider_blocks import BYOK_SUPPORTED_BACKENDS
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.mngr_settings.provider_blocks import cloud_account_provider_name
from imbue.minds.mngr_settings.provider_blocks import remove_provider_block
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings
from imbue.minds.primitives import DEFAULT_AZURE_VM_SIZE
from imbue.minds.primitives import DEFAULT_GCP_MACHINE_TYPE

# The bring-your-own-key cloud-accounts feature ships dark: the create-page UI and the account routes stay hidden until this env var is ``"1"``.
# It is set by the Electron shell (``env`` block in ``electron/backend.js``) for opted-in builds, or exported ambiently for dev -- mirroring how ``SKIP_AUTH`` is read.
_BYOK_CLOUDS_FEATURE_FLAG_ENV: Final[str] = "FEATURE_FLAG_BRING_YOUR_OWN_CLOUDS"


def is_bring_your_own_cloud_enabled() -> bool:
    """Whether the bring-your-own-key cloud-accounts feature is turned on (off by default)."""
    return os.getenv(_BYOK_CLOUDS_FEATURE_FLAG_ENV, "0") == "1"


def set_cloud_account_provider(
    alias: str,
    backend: str,
    credentials: Mapping[str, str],
    region: str,
    *,
    root: MindsRoot,
) -> str:
    """Register a bring-your-own-key cloud account as ``[providers.byok-<backend>-<slug>]``.

    ``credentials`` are the backend's pasted-credential config fields verbatim; they land as plaintext TOML the same way the imbue_cloud session store persists its secrets (0600-class local files).
    The block also pins the minds workspace shape (instance type + gVisor hardening).
    Returns the block name (the mngr provider-instance name that creates will target).

    Raises ``MindsSettingsError`` for an unsupported backend, an unusable alias, a duplicate account name, or an uninitialized mngr profile.
    """
    if backend not in BYOK_SUPPORTED_BACKENDS:
        raise MindsSettingsError(
            f"Unsupported cloud account backend {backend!r} (supported: {BYOK_SUPPORTED_BACKENDS})"
        )
    ensure_mngr_settings(root)
    store = settings_store_for(root)
    if store is None:
        raise MindsSettingsError("mngr is not initialized yet; cannot register a cloud account")
    provider_name = cloud_account_provider_name(backend, alias)

    store.update(
        lambda doc: _register_cloud_account_block(
            doc,
            provider_name=provider_name,
            alias=alias,
            backend=backend,
            credentials=credentials,
            region=region,
        )
    )
    logger.debug("Cloud account {} ({}) registered in {}", provider_name, backend, store.settings_path)
    return provider_name


def list_cloud_account_providers(*, root: MindsRoot) -> list[CloudAccountRecord]:
    """List the registered bring-your-own-key cloud accounts.

    Returns ``[]`` when mngr isn't initialized yet or no accounts exist.
    """
    store = settings_store_for(root)
    if store is None:
        return []
    providers = store.read().get("providers")
    if not isinstance(providers, dict):
        return []
    accounts: list[CloudAccountRecord] = []
    for raw_name, raw_block in sorted(providers.items()):
        # ``isinstance(providers, dict)`` narrows only to dict[object, object]; re-establish str keys / dict blocks for the type checker (tomllib guarantees str keys at runtime).
        name = str(raw_name)
        if not name.startswith(BYOK_PROVIDER_NAME_PREFIX) or not isinstance(raw_block, dict):
            continue
        block: dict[str, object] = {str(key): value for key, value in raw_block.items()}
        accounts.append(
            CloudAccountRecord(
                name=name,
                # Display name = the slug of the user's chosen alias (the part after ``byok-<backend>-``); no separate alias store exists.
                alias=name.split("-", 2)[2] if name.count("-") >= 2 else name,
                backend=str(block.get("backend", "")),
                # GCE is zonal: the GCP block pins default_zone, not default_region.
                region=str(block.get("default_region") or block.get("default_zone") or ""),
                identifier=_cloud_account_identifier(block),
            )
        )
    return accounts


def _cloud_account_identifier(block: Mapping[str, object]) -> str:
    """Masked, display-safe credential hint per backend; never a secret.

    AWS: the access key id masked to its ends.
    GCP: the service account email embedded in the pasted key JSON (an identifier, not a secret).
    Azure: the service principal's client id (a plain identifier).
    """
    backend = str(block.get("backend", ""))
    if backend == "aws":
        key_id = str(block.get("aws_access_key_id", ""))
        return f"{key_id[:4]}…{key_id[-4:]}" if len(key_id) >= 12 else key_id
    if backend == "gcp":
        try:
            key_info = json.loads(str(block.get("service_account_key_json", "")))
        except json.JSONDecodeError as e:
            logger.warning("Cloud account block holds unparseable service_account_key_json: {}", e)
            return ""
        return str(key_info.get("client_email", "")) if isinstance(key_info, dict) else ""
    if backend == "azure":
        return str(block.get("client_id", ""))
    return ""


def delete_cloud_account_provider(provider_name: str, *, root: MindsRoot) -> bool:
    """Remove a cloud account block from minds' settings.

    Only deletes ``byok-*`` blocks -- never the ambient/reconciled providers.
    Cloud-side resources (security group, state bucket) are deliberately left in place; ``mngr <backend> cleanup`` is the explicit teardown for those.
    Returns ``True`` when the file was modified.
    """
    if not provider_name.startswith(BYOK_PROVIDER_NAME_PREFIX):
        return False
    store = settings_store_for(root)
    if store is None:
        return False

    is_modified = store.update(lambda doc: remove_provider_block(doc, provider_name))
    if is_modified:
        logger.debug("Cloud account {} removed from {}", provider_name, store.settings_path)
    return is_modified


def _register_cloud_account_block(
    doc: tomlkit.TOMLDocument,
    *,
    provider_name: str,
    alias: str,
    backend: str,
    credentials: Mapping[str, str],
    region: str,
) -> bool:
    providers = doc.setdefault("providers", tomlkit.table())
    if provider_name in providers:
        raise MindsSettingsError(f"A cloud account named {alias!r} already exists ({provider_name})")
    block = tomlkit.table()
    block["backend"] = backend
    # Every vps-based backend gets the user-data layout knobs (see provider_blocks).
    block["host_dir"] = WORKSPACE_HOST_DIR
    block["volume_home_path"] = WORKSPACE_VOLUME_HOME_PATH
    block["host_log_dir"] = WORKSPACE_HOST_LOG_DIR
    # Per-backend placement + shape.
    # AWS keeps the gVisor hardening knobs; GCP / Azure run the providers' default docker runtime (their templates' hardening args are runtime-agnostic).
    # GCE is zonal, so the GCP "region" value is a zone.
    if backend == "aws":
        block["default_region"] = region
        block["default_instance_type"] = AWS_DEFAULT_INSTANCE_TYPE
        block["install_gvisor_runtime"] = AWS_INSTALL_GVISOR_RUNTIME
        block["docker_runtime"] = AWS_DOCKER_RUNTIME
        block["default_start_args"] = list(AWS_DEFAULT_START_ARGS)
    elif backend == "gcp":
        block["default_zone"] = region
        block["default_machine_type"] = DEFAULT_GCP_MACHINE_TYPE
    else:
        block["default_region"] = region
        block["default_vm_size"] = DEFAULT_AZURE_VM_SIZE
        # Azure's scaffolding (resource group / vnet / NSG) is region-locked, so each account entry is pinned to its region for life and gets its own resource group (named per entry + region).
        # A user who wants another region adds another entry (same keys) -- the per-entry group names let them coexist with no cross-region conflicts.
        block["resource_group"] = f"{provider_name}-{region}"
    for key, value in credentials.items():
        if value:
            block[key] = value
    providers[provider_name] = block
    return True
