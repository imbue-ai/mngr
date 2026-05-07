"""Translate MINDS_ROOT_NAME into MNGR_HOST_DIR and MNGR_PREFIX.

This must run before any ``imbue.mngr.*`` module is imported, because mngr reads
``MNGR_HOST_DIR`` and ``MNGR_PREFIX`` during its own module-level initialization
(plugin manager construction, config discovery, etc.).

Kept intentionally minimal -- only stdlib and loguru -- so it stays cheap to
import and cannot accidentally pull in mngr before translation happens.
"""

import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Final

import tomlkit
from loguru import logger

MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds"
MINDS_ROOT_NAME_PATTERN: Final[str] = r"[a-z0-9_-]+"


def resolve_minds_root_name() -> str:
    """Read MINDS_ROOT_NAME from the environment or return the default.

    Validates the value against MINDS_ROOT_NAME_PATTERN and exits with status
    1 if invalid. Validation is duplicated here (instead of going through a
    pydantic primitive) so this module never has to import pydantic/mngr.
    """
    value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR, DEFAULT_MINDS_ROOT_NAME)
    if not re.fullmatch(MINDS_ROOT_NAME_PATTERN, value):
        logger.error("{} must match {!r}; got {!r}", MINDS_ROOT_NAME_ENV_VAR, MINDS_ROOT_NAME_PATTERN, value)
        sys.exit(1)
    return value


def minds_data_dir_for(root_name: str) -> Path:
    """Return the minds data directory for a given root name (e.g. ~/.minds)."""
    return Path.home() / ".{}".format(root_name)


def mngr_host_dir_for(root_name: str) -> Path:
    """Return the mngr host directory for a given root name (e.g. ~/.minds/mngr)."""
    return minds_data_dir_for(root_name) / "mngr"


def mngr_prefix_for(root_name: str) -> str:
    """Return the mngr prefix for a given root name (e.g. minds-)."""
    return "{}-".format(root_name)


def _ensure_mngr_settings(root_name: str) -> None:
    """Ensure the mngr settings.toml has minds-side overrides configured.

    Disables the ``recursive`` plugin for every ``mngr`` subprocess minds
    spawns. ``mngr_recursive``'s ``on_host_created`` hook injects the
    calling user's local ``~/.claude/`` and ``~/.mngr/`` deploy files
    into the workspace, which contradicts the contract that the repo
    (whatever git URL/branch the user picked) is the full definition
    of the workspace. minds runs inside its own ``MNGR_HOST_DIR``
    profile, so flipping the plugin off here only affects
    minds-spawned subprocesses; CLI-side mngr usage from other
    host_dirs is unaffected.

    The TOML key under ``[plugins]`` must match the pluggy entry-point
    name (``recursive``), not the package name (``mngr_recursive``).
    ``mngr/libs/mngr/imbue/mngr/config/pre_readers.py`` reads section
    names verbatim and ``pm.set_blocked`` matches by the exact
    registered name.

    Also tears down any vestige of the older "leased-host SSH dance":
    a previous version of minds wrote a ``[providers.ssh]`` block here
    pointing at a ``dynamic_hosts.toml`` populated by the lease flow.
    The imbue_cloud provider plugin owns that path now (it talks to
    the connector service directly, not through an SSH-provider side
    channel), so the SSH provider block + dynamic_hosts.toml are pure
    leak: stale entries in dynamic_hosts.toml caused ``mngr list``
    discovery to time out trying to ssh-connect to long-destroyed VPS
    IPs. We remove the section here so ``mngr list`` only fans out to
    real providers, and delete the stale data file (and its associated
    leased-host SSH key dir) so even direct readers see a clean slate.

    Skips silently when mngr hasn't been initialized in this host_dir
    yet (no ``config.toml`` / no profile dir) -- there's nothing to
    write to.
    """
    mngr_host_dir = mngr_host_dir_for(root_name)
    root_config_path = mngr_host_dir / "config.toml"
    if not root_config_path.exists():
        return
    root_config = tomllib.loads(root_config_path.read_text())
    profile_id = root_config.get("profile")
    if not profile_id:
        return
    settings_dir = mngr_host_dir / "profiles" / profile_id
    if not settings_dir.exists():
        return
    settings_path = settings_dir / "settings.toml"

    if settings_path.exists():
        existing = tomllib.loads(settings_path.read_text())
        providers = existing.get("providers", {})
        plugins = existing.get("plugins", {})
        recursive_plugin = plugins.get("recursive", {})
        default_imbue_cloud = providers.get(_IMBUE_CLOUD_BACKEND_NAME, {})
        if (
            recursive_plugin.get("enabled") is False
            and "ssh" not in providers
            and default_imbue_cloud.get("backend") == _IMBUE_CLOUD_BACKEND_NAME
            and default_imbue_cloud.get("is_enabled") is False
        ):
            # Already in the desired shape -- recursive disabled, no stale
            # ssh provider section, default imbue_cloud instance suppressed --
            # no need to rewrite + fsync.
            _cleanup_legacy_dynamic_hosts(root_name)
            return
        doc = tomlkit.loads(settings_path.read_text())
    else:
        doc = tomlkit.document()

    providers_section = doc.setdefault("providers", tomlkit.table())

    # Remove the legacy ``[providers.ssh]`` block, if present, so ``mngr list``
    # discovery doesn't fan out to that provider's stale dynamic_hosts entries.
    if "ssh" in providers_section:
        del providers_section["ssh"]

    # Suppress the default ``[providers.imbue_cloud]`` instance that
    # ``get_all_provider_instances`` would otherwise auto-create from the
    # registered backend name. Per-account ``[providers.imbue_cloud_<slug>]``
    # entries (written on signin) carry the actual session keypairs and
    # known_hosts; the un-suffixed default would race them and emit
    # spurious "No host key in known_hosts" warnings on the same lease.
    default_block = tomlkit.table()
    default_block["backend"] = _IMBUE_CLOUD_BACKEND_NAME
    default_block["is_enabled"] = False
    providers_section[_IMBUE_CLOUD_BACKEND_NAME] = default_block

    plugins_section = doc.setdefault("plugins", tomlkit.table())
    recursive_block = tomlkit.table()
    recursive_block["enabled"] = False
    plugins_section["recursive"] = recursive_block

    settings_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_suffix(".tmp")
    tmp_path.write_text(tomlkit.dumps(doc))
    tmp_path.rename(settings_path)
    logger.debug("Updated mngr settings at {} with minds-side overrides", settings_path)
    _cleanup_legacy_dynamic_hosts(root_name)


def _cleanup_legacy_dynamic_hosts(root_name: str) -> None:
    """Remove the stale ``ssh/dynamic_hosts.toml`` file + ``ssh/keys/leased_host/`` dir.

    Both are vestigial: the imbue_cloud provider replaces the leased-host
    SSH-provider mechanism entirely, but minds installations from before
    that refactor still have these files lying around. The
    ``dynamic_hosts.toml`` file in particular contains entries pointing
    at long-destroyed VPS IPs, and any code path that reads it would
    block on TCP timeouts. Best-effort: log + continue on any FS error.
    """
    data_dir = minds_data_dir_for(root_name)
    legacy_paths = (
        data_dir / "ssh" / "dynamic_hosts.toml",
        data_dir / "ssh" / "keys" / "leased_host",
    )
    for path in legacy_paths:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:
            logger.warning("Could not remove legacy minds-leased-host artifact {}: {}", path, e)
        else:
            logger.info("Removed legacy minds-leased-host artifact {}", path)


def apply_bootstrap() -> None:
    """Set MNGR_HOST_DIR and MNGR_PREFIX in os.environ from MINDS_ROOT_NAME.

    Must be called before any ``imbue.mngr.*`` module is imported. When
    ``MINDS_ROOT_NAME`` is explicitly set in the environment, the derived
    ``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` values unconditionally override
    any pre-existing values -- otherwise an inherited ``MNGR_HOST_DIR``
    from a parent process (e.g. a Claude Code agent's tmux env) would
    silently win and minds would read a different mngr settings.toml
    than the bootstrap wrote to. When ``MINDS_ROOT_NAME`` is not set,
    the defaults are written via ``setdefault`` so test fixtures and
    advanced users who pin ``MNGR_HOST_DIR`` directly can still do so.

    Also reconciles the imbue_cloud provider entries in mngr's settings.toml
    against the persistent session list so a user with a still-valid
    SuperTokens cookie always has a usable ``[providers.imbue_cloud_<slug>]``
    block for ``mngr create`` -- previously the entry was only written by
    a fresh signin event, so any drift (older bootstrap bug, manual edit,
    deleted-then-recreated settings.toml, etc.) left the user able to
    sign in but unable to create a workspace until they explicitly
    signed out and back in.
    """
    is_root_name_explicit = MINDS_ROOT_NAME_ENV_VAR in os.environ
    root_name = resolve_minds_root_name()
    if is_root_name_explicit:
        os.environ["MNGR_HOST_DIR"] = str(mngr_host_dir_for(root_name))
        os.environ["MNGR_PREFIX"] = mngr_prefix_for(root_name)
    else:
        os.environ.setdefault("MNGR_HOST_DIR", str(mngr_host_dir_for(root_name)))
        os.environ.setdefault("MNGR_PREFIX", mngr_prefix_for(root_name))
    _ensure_mngr_settings(root_name)
    _reconcile_imbue_cloud_providers_from_sessions(root_name)


def _reconcile_imbue_cloud_providers_from_sessions(root_name: str) -> None:
    """Re-register ``[providers.imbue_cloud_<slug>]`` for every active session.

    minds' SuperTokens session is persistent: the auth cookie + the entry
    in ``<minds_data_dir>/sessions.json`` outlive any individual minds
    process. The mngr-side provider-instance registration in settings.toml
    isn't persistent the same way -- it's only written by the signin
    *event*, which doesn't fire on cookie-resumed startups. So it's
    possible (and was observed) for the on-disk state to drift to "user
    is signed in per sessions.json, but settings.toml has no
    [providers.imbue_cloud_<email-slug>] block", at which point
    ``mngr create mindtest@<host>.imbue_cloud_<slug>`` fails with
    ``Unknown provider backend``.

    Walking sessions.json on every minds startup and ensuring each email
    has a registered provider entry costs essentially nothing
    (``set_imbue_cloud_provider_for_account`` is a no-op when the entry
    already matches) and makes the bootstrap idempotent over arbitrary
    settings.toml drift.

    No-op when sessions.json doesn't exist yet (e.g. a fresh install
    where the user hasn't signed in at all).
    """
    sessions_path = minds_data_dir_for(root_name) / "sessions.json"
    if not sessions_path.is_file():
        return
    try:
        raw = sessions_path.read_text()
    except OSError as e:
        logger.warning("Could not read minds sessions file {}: {}", sessions_path, e)
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Malformed minds sessions file {}: {}", sessions_path, e)
        return
    if not isinstance(data, dict):
        return
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not isinstance(email, str) or not email:
            continue
        try:
            # Reconcile only fills in missing blocks; it must not re-enable a
            # provider that was previously auto-disabled by an auth-error
            # observation. Re-enable happens only on an explicit signin
            # event.
            set_imbue_cloud_provider_for_account(email, root_name=root_name, force_enable=False)
        except BootstrapError as e:
            # Bad email format (e.g. ``""``) -- log and keep going so a
            # single corrupt session entry doesn't block reconciliation
            # for the others.
            logger.warning("Skipping imbue_cloud provider registration for {!r}: {}", email, e)


_IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"


class BootstrapError(ValueError):
    """Raised when minds bootstrap can't compute a derived value (e.g. a slug from an empty email).

    Defined locally instead of importing ``minds.errors`` because this
    module has to stay free of any ``imbue.mngr.*`` / ``click`` imports
    (see the module docstring).
    """


def _slugify_imbue_cloud_account(email: str) -> str:
    """Mirror the plugin's ``slugify_account``.

    Inlined so this module stays mngr-free (it has to be importable before
    ``imbue.mngr`` is on sys.path).
    """
    lowered = email.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise BootstrapError(f"Cannot slugify imbue_cloud account email: {email!r}")
    return slug


def imbue_cloud_provider_name_for_account(email: str) -> str:
    """Return the provider instance name minds writes for ``email``."""
    return f"imbue_cloud_{_slugify_imbue_cloud_account(email)}"


def _resolve_active_settings_path(root_name: str) -> Path | None:
    """Locate the active mngr settings.toml under the minds host_dir.

    Returns ``None`` if mngr hasn't been initialized in this host_dir yet
    (e.g. minds was just installed and no command has materialized
    ``config.toml`` / a profile dir). Callers should treat ``None`` as
    "skip silently" since there's nothing useful to write yet.
    """
    mngr_host_dir = mngr_host_dir_for(root_name)
    root_config_path = mngr_host_dir / "config.toml"
    if not root_config_path.exists():
        return None
    root_config = tomllib.loads(root_config_path.read_text())
    profile_id = root_config.get("profile")
    if not profile_id:
        return None
    settings_dir = mngr_host_dir / "profiles" / profile_id
    if not settings_dir.exists():
        return None
    return settings_dir / "settings.toml"


def _atomic_write_settings(settings_path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Write ``doc`` to ``settings_path`` via a tmp-file + rename.

    Atomic so a concurrent reader never sees a half-written file.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_suffix(".tmp")
    tmp_path.write_text(tomlkit.dumps(doc))
    tmp_path.rename(settings_path)


def set_imbue_cloud_provider_for_account(
    email: str,
    *,
    root_name: str | None = None,
    force_enable: bool = True,
) -> bool:
    """Register ``[providers.imbue_cloud_<slug>]`` in mngr's settings.toml.

    Called by minds when a SuperTokens session for ``email`` is created
    (signin/signup/oauth-success) and from the bootstrap reconcile. Idempotent:
    a no-op if an equivalent entry already exists.

    When ``force_enable`` is True (signin events), ``is_enabled`` is set to
    True even if the block was previously disabled by an auth-error
    auto-disable. When False (bootstrap reconcile on a returning user),
    any pre-existing ``is_enabled`` value is preserved so a previously
    auto-disabled account stays disabled until the user signs in again.

    Returns ``True`` when the file was modified, so callers know whether
    to bounce ``mngr observe`` (the running process needs a restart to
    see the new provider instance).
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    settings_path = _resolve_active_settings_path(root_name)
    if settings_path is None:
        return False
    provider_name = imbue_cloud_provider_name_for_account(email)
    if settings_path.exists():
        doc = tomlkit.loads(settings_path.read_text())
    else:
        doc = tomlkit.document()
    providers = doc.setdefault("providers", tomlkit.table())
    existing = providers.get(provider_name)
    existing_is_enabled = existing.get("is_enabled") if isinstance(existing, dict) else None
    desired_is_enabled = True if force_enable else existing_is_enabled
    if (
        isinstance(existing, dict)
        and existing.get("backend") == _IMBUE_CLOUD_BACKEND_NAME
        and existing.get("account") == email
        and existing_is_enabled == desired_is_enabled
    ):
        return False
    new_block = tomlkit.table()
    new_block["backend"] = _IMBUE_CLOUD_BACKEND_NAME
    new_block["account"] = email
    if desired_is_enabled is not None:
        new_block["is_enabled"] = desired_is_enabled
    providers[provider_name] = new_block
    _atomic_write_settings(settings_path, doc)
    logger.info("imbue_cloud provider {} registered in {}", provider_name, settings_path)
    return True


def is_imbue_cloud_provider_enabled_for_account(email: str, *, root_name: str | None = None) -> bool:
    """Return whether ``[providers.imbue_cloud_<slug>]`` is currently enabled.

    Reads the ``is_enabled`` field from the active mngr settings.toml so
    the desktop UI can render "Signed out" on a chip whose provider was
    auto-disabled by an observed auth error. Treats a missing entry or
    a missing ``is_enabled`` field as enabled (per mngr's default), and
    returns True when the settings file can't be located so the UI never
    erroneously claims an account is signed out before the bootstrap
    has finished writing the block.
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    settings_path = _resolve_active_settings_path(root_name)
    if settings_path is None or not settings_path.exists():
        return True
    try:
        provider_name = imbue_cloud_provider_name_for_account(email)
    except BootstrapError:
        return True
    parsed = tomllib.loads(settings_path.read_text())
    providers = parsed.get("providers")
    if not isinstance(providers, dict):
        return True
    block = providers.get(provider_name)
    if not isinstance(block, dict):
        return True
    is_enabled = block.get("is_enabled", True)
    return bool(is_enabled)


def disable_imbue_cloud_provider_for_account(email: str, *, root_name: str | None = None) -> bool:
    """Mark ``[providers.imbue_cloud_<slug>]`` as ``is_enabled = false``.

    Called by minds when discovery surfaces an unrecoverable auth error
    for ``email`` (e.g. SuperTokens "token theft detected", or the refresh
    token expiring past the family lifetime). Disabling the entry makes
    subsequent ``mngr observe`` cycles skip this provider so the rest of
    the discovery pipeline keeps working until the user signs in again.
    Idempotent: a no-op if the block doesn't exist or is already
    disabled. Returns ``True`` when the file was modified.
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    settings_path = _resolve_active_settings_path(root_name)
    if settings_path is None or not settings_path.exists():
        return False
    provider_name = imbue_cloud_provider_name_for_account(email)
    doc = tomlkit.loads(settings_path.read_text())
    providers = doc.get("providers")
    if not isinstance(providers, dict):
        return False
    existing = providers.get(provider_name)
    if not isinstance(existing, dict):
        return False
    if existing.get("is_enabled") is False:
        return False
    existing["is_enabled"] = False
    _atomic_write_settings(settings_path, doc)
    logger.info("imbue_cloud provider {} disabled in {}", provider_name, settings_path)
    return True


def unset_imbue_cloud_provider_for_account(email: str, *, root_name: str | None = None) -> bool:
    """Remove ``[providers.imbue_cloud_<slug>]`` from mngr's settings.toml.

    Called by minds on signout. Idempotent: a no-op if no such entry
    exists. Returns ``True`` when the file was modified.
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    settings_path = _resolve_active_settings_path(root_name)
    if settings_path is None or not settings_path.exists():
        return False
    provider_name = imbue_cloud_provider_name_for_account(email)
    doc = tomlkit.loads(settings_path.read_text())
    providers = doc.get("providers")
    if not isinstance(providers, dict) or provider_name not in providers:
        return False
    del providers[provider_name]
    _atomic_write_settings(settings_path, doc)
    logger.info("imbue_cloud provider {} removed from {}", provider_name, settings_path)
    return True
