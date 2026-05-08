"""Plugin entry point: registers the provider backend and CLI commands."""

import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
from loguru import logger

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud import hookimpl
from imbue.mngr_imbue_cloud.backend import ImbueCloudProviderBackend
from imbue.mngr_imbue_cloud.cli.root import imbue_cloud as imbue_cloud_group
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the imbue_cloud provider backend."""
    return (ImbueCloudProviderBackend, ImbueCloudProviderConfig)


@hookimpl
def register_cli_commands() -> Sequence[click.Command]:
    """Register the top-level `mngr imbue_cloud` command group."""
    return [imbue_cloud_group]


@hookimpl
def on_load_config(config_dict: dict[str, Any]) -> None:
    """Auto-disable the default ``[providers.imbue_cloud]`` instance when no
    accounts are signed in.

    The provider backend registers a default instance (named after its backend)
    via ``get_all_provider_instances``. If no account is signed in, calling
    ``mngr list`` (or any discovery path) raises ``MngrError`` with no actionable
    config. To match the "skip silently when not configured" behavior of the
    other remote backends (modal/vultr), we inject ``[providers.imbue_cloud]
    is_enabled = false`` here whenever no sessions exist.

    Skipped when the user has explicitly configured ``[providers.imbue_cloud]``
    themselves -- only the implicit default is suppressed. Per-account names
    like ``[providers.imbue_cloud_alice]`` are unaffected because we key on the
    bare backend name only.
    """
    providers: dict[ProviderInstanceName, ProviderInstanceConfig] = config_dict.get("providers") or {}
    default_name = ProviderInstanceName(IMBUE_CLOUD_BACKEND_NAME)
    if default_name in providers:
        return

    if _has_signed_in_accounts(config_dict.get("default_host_dir")):
        return

    providers[default_name] = ImbueCloudProviderConfig(
        backend=ProviderBackendName(IMBUE_CLOUD_BACKEND_NAME),
        is_enabled=False,
    )
    config_dict["providers"] = providers


def _has_signed_in_accounts(default_host_dir: Any) -> bool:
    """Return True if any imbue_cloud session is on disk under ``default_host_dir``.

    Resolves the active profile dir from ``<host_dir>/config.toml`` and probes
    its sessions directory. Any failure to locate or read the directory is
    treated as "no accounts" -- the caller will then suppress the default
    provider, which is the safe behavior in fresh / test environments.
    """
    if default_host_dir is None:
        return False
    profile_dir = _resolve_profile_dir(default_host_dir)
    if profile_dir is None:
        return False
    try:
        session_store = ImbueCloudSessionStore(sessions_dir=get_sessions_dir(profile_dir))
        return bool(session_store.list_accounts())
    except OSError as exc:
        logger.debug("imbue_cloud on_load_config skipped session probe: {}", exc)
        return False


def _resolve_profile_dir(default_host_dir: Any) -> Path | None:
    """Return the active mngr profile dir under ``default_host_dir``, or None.

    Mirrors the resolution rules in ``ImbueCloudProviderConfig``-adjacent
    helpers: read ``<host_dir>/config.toml``, look up the ``profile`` field,
    and return ``<host_dir>/profiles/<id>``. Returns ``None`` (so the caller
    can no-op) when mngr hasn't been initialized in this host_dir yet.
    """
    host_dir = Path(default_host_dir).expanduser()
    config_path = host_dir / "config.toml"
    if not config_path.exists():
        return None
    try:
        root_config = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("imbue_cloud on_load_config: cannot read {} ({})", config_path, exc)
        return None
    profile_id = root_config.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return None
    return host_dir / "profiles" / profile_id
