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
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import tomlkit
from loguru import logger
from tomlkit.items import Table

from imbue.minds.primitives import CONFIGURED_AWS_REGIONS

MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds"
# Names that are not legal env-name suffixes. Today this is just the prefix
# string itself, because ``minds-`` with an empty suffix would round-trip
# to the production path (``~/.minds-/`` is nonsensical) and we'd rather
# fail loudly than silently coerce.
_MINDS_PREFIX: Final[str] = "minds"
# Legal env-name suffixes after ``minds-``. Mirrors the rules in
# :mod:`imbue.minds.envs.primitives` and the reserved tier names in
# :mod:`imbue.minds.cli.env`:
#
#   * ``staging`` -- the reserved staging tier name.
#   * ``dev-<rest>`` / ``ci-<rest>`` -- any dynamic env (developer dev
#     env or CI ephemeral env, respectively). Together they mirror
#     :data:`imbue.minds.envs.primitives.DEV_ENV_NAME_PATTERN`; kept
#     inlined here so this module stays free of ``imbue.mngr.*`` /
#     pydantic imports (see module docstring).
#
# Production has no suffix (``minds`` alone). Anything that does not
# fit this pattern is treated as ``unset`` by ``resolve_minds_root_name``
# and falls back to production with a warning.
_STAGING_SUFFIX_PATTERN: Final[str] = r"staging"
_DYNAMIC_SUFFIX_PATTERN: Final[str] = r"(?:dev|ci)-[a-z0-9][a-z0-9_-]{0,33}[a-z0-9]"
_ENV_NAME_PATTERN: Final[str] = rf"(?:{_STAGING_SUFFIX_PATTERN}|{_DYNAMIC_SUFFIX_PATTERN})"
# The full set of legal MINDS_ROOT_NAME values is ``minds`` (production),
# ``minds-staging``, ``minds-dev-<rest>``, or ``minds-ci-<rest>``.
MINDS_ROOT_NAME_PATTERN: Final[str] = rf"{_MINDS_PREFIX}(-{_ENV_NAME_PATTERN})?"


def resolve_minds_root_name() -> str:
    """Read MINDS_ROOT_NAME from the environment or return the default.

    Validates the value against :data:`MINDS_ROOT_NAME_PATTERN`. When the
    env var is unset, returns :data:`DEFAULT_MINDS_ROOT_NAME` (production).
    When the env var holds a value that does not match the pattern (e.g.
    a stale ``devminds`` left in a parent shell from before the
    per-env-root refactor), logs a warning and returns the default --
    callers that genuinely need an activated env check explicitly via
    :func:`is_minds_root_name_set_to_active_env` instead.

    Validation is duplicated here (instead of going through a pydantic
    primitive) so this module never has to import pydantic/mngr.
    """
    value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR)
    if value is None:
        return DEFAULT_MINDS_ROOT_NAME
    if not re.fullmatch(MINDS_ROOT_NAME_PATTERN, value):
        logger.warning(
            "{}={!r} does not match {!r}; ignoring and falling back to {!r}. "
            'Run `eval "$(minds env activate <name>)"` to activate a valid env.',
            MINDS_ROOT_NAME_ENV_VAR,
            value,
            MINDS_ROOT_NAME_PATTERN,
            DEFAULT_MINDS_ROOT_NAME,
        )
        return DEFAULT_MINDS_ROOT_NAME
    return value


def is_minds_root_name_set_to_active_env() -> bool:
    """Return True iff ``MINDS_ROOT_NAME`` is explicitly set to a valid value.

    Used by ``minds env deploy/destroy`` and ``minds run`` to refuse when
    no env has been activated. Distinguishes "operator forgot to activate"
    (unset / invalid -> False) from "operator activated production"
    (``MINDS_ROOT_NAME=minds`` -> True). Treats values that don't match
    :data:`MINDS_ROOT_NAME_PATTERN` as "not activated" because they get
    silently overridden by :func:`resolve_minds_root_name`.
    """
    value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR)
    if value is None:
        return False
    return re.fullmatch(MINDS_ROOT_NAME_PATTERN, value) is not None


def env_name_from_root_name(root_name: str) -> str:
    """Return the env name for a given ``MINDS_ROOT_NAME``.

    ``minds`` -> ``production``; ``minds-<name>`` -> ``<name>``. Raises
    ``BootstrapError`` for any other value -- callers should validate via
    :func:`resolve_minds_root_name` first.
    """
    if root_name == DEFAULT_MINDS_ROOT_NAME:
        return "production"
    if not root_name.startswith(f"{_MINDS_PREFIX}-"):
        raise BootstrapError(
            f"Cannot extract env name from {MINDS_ROOT_NAME_ENV_VAR}={root_name!r}: "
            f"expected {DEFAULT_MINDS_ROOT_NAME!r} or {_MINDS_PREFIX}-<env-name>."
        )
    return root_name[len(_MINDS_PREFIX) + 1 :]


def root_name_for_env_name(env_name: str) -> str:
    """Return the ``MINDS_ROOT_NAME`` value for a given env name.

    ``production`` -> ``minds``; anything else -> ``minds-<name>``. The
    env name is not re-validated here; callers should validate via
    :class:`imbue.minds.envs.primitives.DevEnvName` first.
    """
    if env_name == "production":
        return DEFAULT_MINDS_ROOT_NAME
    return f"{_MINDS_PREFIX}-{env_name}"


def minds_data_dir_for(root_name: str) -> Path:
    """Return the minds data directory for a given root name (e.g. ~/.minds)."""
    return Path.home() / ".{}".format(root_name)


def mngr_host_dir_for(root_name: str) -> Path:
    """Return the mngr host directory for a given root name (e.g. ~/.minds/mngr)."""
    return minds_data_dir_for(root_name) / "mngr"


def mngr_prefix_for(root_name: str) -> str:
    """Return the mngr prefix for a given root name (e.g. minds-)."""
    return "{}-".format(root_name)


def _aws_credentials_plausibly_configured() -> bool:
    """Cheap heuristic for whether boto3 would find AWS credentials, without importing boto3.

    Mirrors the legs of boto3's default credential chain that apply on a
    developer laptop (``AWS_*`` env vars, ``AWS_PROFILE``, ``~/.aws`` files) --
    minds runs on the user's machine, not on EC2, so the IMDS leg is
    irrelevant. Gates whether the per-region ``[providers.aws-<region>]`` blocks
    are written: writing them with no credentials present would make every
    ``mngr list`` fan out to dead AWS providers and log a provider-unavailable
    error per region.
    """
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return True
    aws_dir = Path.home() / ".aws"
    return (aws_dir / "credentials").is_file() or (aws_dir / "config").is_file()


def _desired_aws_provider_names() -> tuple[str, ...]:
    """Return the ``aws-<region>`` provider names minds should configure, or () when AWS is unconfigured."""
    if not _aws_credentials_plausibly_configured():
        return ()
    return tuple(f"{_AWS_PROVIDER_NAME_PREFIX}{region}" for region in CONFIGURED_AWS_REGIONS)


def _existing_aws_provider_names(providers_mapping: Mapping[str, object]) -> set[str]:
    """Return the set of ``aws-<region>`` provider names currently present in a providers mapping."""
    return {name for name in providers_mapping if name.startswith(_AWS_PROVIDER_NAME_PREFIX)}


def _write_aws_provider_blocks(providers_section: Table, desired_names: tuple[str, ...]) -> None:
    """Rewrite the ``[providers.aws-<region>]`` blocks so they exactly match ``desired_names``.

    Removes any stale ``aws-<region>`` blocks (AWS credentials removed, or
    ``CONFIGURED_AWS_REGIONS`` changed) and (re)writes one block per desired
    name, pinning the backend to ``aws``, the region to the name's suffix, and
    the gVisor/runsc hardening knobs that mirror the ovh/vultr bake settings.
    """
    for name in tuple(providers_section):
        if name.startswith(_AWS_PROVIDER_NAME_PREFIX):
            del providers_section[name]
    for name in desired_names:
        region = name[len(_AWS_PROVIDER_NAME_PREFIX) :]
        block = tomlkit.table()
        block["backend"] = _AWS_BACKEND_NAME
        block["default_region"] = region
        block["default_instance_type"] = _AWS_DEFAULT_INSTANCE_TYPE
        block["install_gvisor_runtime"] = _AWS_INSTALL_GVISOR_RUNTIME
        block["docker_runtime"] = _AWS_DOCKER_RUNTIME
        providers_section[name] = block


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

    # The per-region AWS provider blocks minds should currently have configured
    # (one per region when AWS credentials are present, none otherwise).
    desired_aws_names = _desired_aws_provider_names()

    if settings_path.exists():
        existing = tomllib.loads(settings_path.read_text())
        providers = existing.get("providers", {})
        plugins = existing.get("plugins", {})
        recursive_plugin = plugins.get("recursive", {})
        default_imbue_cloud = providers.get(_IMBUE_CLOUD_BACKEND_NAME, {})
        default_aws = providers.get(_AWS_BACKEND_NAME, {})
        if (
            recursive_plugin.get("enabled") is False
            and "ssh" not in providers
            and default_imbue_cloud.get("backend") == _IMBUE_CLOUD_BACKEND_NAME
            and default_imbue_cloud.get("is_enabled") is False
            and default_aws.get("backend") == _AWS_BACKEND_NAME
            and default_aws.get("is_enabled") is False
            and _existing_aws_provider_names(providers) == set(desired_aws_names)
        ):
            # Already in the desired shape -- recursive disabled, no stale
            # ssh provider section, default imbue_cloud + aws instances
            # suppressed -- no need to rewrite + fsync.
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

    # Suppress the default ``[providers.aws]`` instance for the same reason: the
    # registered ``aws`` backend would otherwise auto-create a region-less
    # provider whose discovery fails every ``mngr list`` cycle ("credentials not
    # configured" -- it has no default_region), logging a spurious warning. The
    # usable providers are the per-region ``aws-<region>`` blocks below. This is
    # written unconditionally (even with no AWS credentials), since the no-creds
    # case is exactly when the region-less default would log on every cycle.
    default_aws_block = tomlkit.table()
    default_aws_block["backend"] = _AWS_BACKEND_NAME
    default_aws_block["is_enabled"] = False
    providers_section[_AWS_BACKEND_NAME] = default_aws_block

    # Write one ``[providers.aws-<region>]`` block per configured region (when
    # AWS credentials are present), so ``mngr create @host.aws-<region>`` and
    # ``mngr list`` discovery both resolve the region-specific provider. When no
    # AWS credentials are configured, ``desired_aws_names`` is empty and any
    # stale blocks are removed.
    _write_aws_provider_blocks(providers_section, desired_aws_names)

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
    ``MINDS_ROOT_NAME`` is set to a valid value (matching
    :data:`MINDS_ROOT_NAME_PATTERN`), the derived ``MNGR_HOST_DIR`` /
    ``MNGR_PREFIX`` values unconditionally override any pre-existing
    values -- otherwise an inherited ``MNGR_HOST_DIR`` from a parent
    process (e.g. a Claude Code agent's tmux env) would silently win and
    minds would read a different mngr settings.toml than the bootstrap
    wrote to.

    When ``MINDS_ROOT_NAME`` is unset, this function leaves
    ``MNGR_HOST_DIR`` / ``MNGR_PREFIX`` untouched -- the per-env-root
    refactor moved env activation to an explicit ``minds env activate``
    step, so an unactivated shell has nothing to seed. Callers that need
    an activated env refuse explicitly (e.g. ``minds run``,
    ``minds env deploy``); callers that only need the production data
    dir (``~/.minds/``) handle that themselves via
    :func:`mngr_host_dir_for` + :data:`DEFAULT_MINDS_ROOT_NAME`.

    When ``MINDS_ROOT_NAME`` is set to a value that does not match
    :data:`MINDS_ROOT_NAME_PATTERN` (e.g. a stale ``devminds`` shell
    from before the refactor), :func:`resolve_minds_root_name` logs a
    warning and returns the default -- we then export the default's
    derived ``MNGR_*`` vars so downstream mngr calls have *some*
    consistent host_dir to point at instead of half-honoring the bad
    value.

    Also reconciles the imbue_cloud provider entries in mngr's
    settings.toml against the persistent session list so a user with a
    still-valid SuperTokens cookie always has a usable
    ``[providers.imbue_cloud_<slug>]`` block for ``mngr create`` --
    previously the entry was only written by a fresh signin event, so
    any drift (older bootstrap bug, manual edit, deleted-then-recreated
    settings.toml, etc.) left the user able to sign in but unable to
    create a workspace until they explicitly signed out and back in.
    """
    raw_value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR)
    if raw_value is None:
        # Unactivated shell: leave MNGR_* alone. The ``mngr`` CLI's own
        # defaults will land it on ``~/.mngr/`` if nothing's pre-set;
        # production-only minds entry points (the bundled Electron build)
        # always set both ``MINDS_ROOT_NAME`` and the derived vars before
        # invoking us, so an unset value here genuinely means "the user
        # has not activated any env yet".
        return
    root_name = resolve_minds_root_name()
    os.environ["MNGR_HOST_DIR"] = str(mngr_host_dir_for(root_name))
    os.environ["MNGR_PREFIX"] = mngr_prefix_for(root_name)
    _ensure_mngr_settings(root_name)
    # Provider reconciliation moved out of apply_bootstrap because it now
    # requires the per-env connector URL; callers (i.e. `minds run`) invoke
    # `reconcile_imbue_cloud_providers_from_sessions(connector_url)` after
    # loading the client config.


def reconcile_imbue_cloud_providers_from_sessions(connector_url: str, *, root_name: str | None = None) -> None:
    """Re-register ``[providers.imbue_cloud_<slug>]`` for every active session.

    The mngr_imbue_cloud plugin owns the SuperTokens session list -- emails
    live in ``<host_dir>/profiles/<profile>/providers/imbue_cloud/sessions/accounts.json``,
    which mngr writes on every signin/signup/oauth and on signout. The
    mngr-side provider-instance registration in settings.toml isn't
    persistent the same way -- it's only written by the signin *event*,
    which doesn't fire on cookie-resumed startups. So it's possible (and
    was observed) for the on-disk state to drift to "user is signed in
    per the plugin, but settings.toml has no
    ``[providers.imbue_cloud_<email-slug>]`` block", at which point
    ``mngr create my-agent@<host>.imbue_cloud_<slug>`` fails with
    ``Unknown provider backend``.

    Walking the plugin's ``accounts.json`` on every minds startup and
    ensuring each email has a registered provider entry costs essentially
    nothing (``set_imbue_cloud_provider_for_account`` is a no-op when the
    entry already matches) and makes the bootstrap idempotent over
    arbitrary settings.toml drift.

    No-op when the accounts file doesn't exist yet (fresh install with no
    signins).
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    # Same rationale as in ``set_imbue_cloud_provider_for_account``: the
    # startup ``apply_bootstrap`` call no-ops on a freshly-created
    # MINDS_ROOT_NAME (mngr profile dir doesn't exist yet). Re-run here
    # so existing users who never re-signin still get the suppression
    # block on their next minds startup.
    _ensure_mngr_settings(root_name)
    accounts_path = _imbue_cloud_accounts_path(root_name)
    if accounts_path is None or not accounts_path.is_file():
        return
    try:
        raw = accounts_path.read_text()
    except OSError as e:
        logger.warning("Could not read imbue_cloud accounts index {}: {}", accounts_path, e)
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Malformed imbue_cloud accounts index {}: {}", accounts_path, e)
        return
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not isinstance(email, str) or not email:
            continue
        try:
            # Reconcile only fills in missing blocks; it must not re-enable a
            # provider that the user previously disabled via the providers
            # panel. Re-enable happens only on an explicit signin event.
            set_imbue_cloud_provider_for_account(
                email,
                connector_url=connector_url,
                root_name=root_name,
                force_enable=False,
            )
        except BootstrapError as e:
            # Bad email format (e.g. ``""``) -- log and keep going so a
            # single corrupt session entry doesn't block reconciliation
            # for the others.
            logger.warning("Skipping imbue_cloud provider registration for {!r}: {}", email, e)


def read_active_profile_dir(mngr_host_dir: Path) -> Path | None:
    """Return ``<mngr_host_dir>/profiles/<active-profile>``, or None if unresolved.

    The active profile id lives in ``<mngr_host_dir>/config.toml`` under the
    ``profile`` key and each profile's state lives at
    ``<mngr_host_dir>/profiles/<profile>/``. Returns None when mngr hasn't been
    initialized in this host_dir yet (no ``config.toml`` / no ``profile`` key) or
    when the config can't be read. Resolution is inlined here (rather than imported
    from mngr) so bootstrap stays free of any ``imbue.mngr.*`` import.
    """
    config_path = mngr_host_dir / "config.toml"
    if not config_path.is_file():
        return None
    try:
        config_data = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read mngr config {}: {}", config_path, e)
        return None
    profile_id = config_data.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return None
    return mngr_host_dir / "profiles" / profile_id


def _imbue_cloud_accounts_path(root_name: str) -> Path | None:
    """Return the path to the plugin's ``accounts.json``, or None if no profile is set.

    Mirrors ``mngr_imbue_cloud.config.get_sessions_dir`` /
    ``get_active_profile_dir``: the active profile id lives in
    ``<host_dir>/config.toml`` and the accounts index lives at
    ``<host_dir>/profiles/<profile>/providers/imbue_cloud/sessions/accounts.json``.
    Inlined here so bootstrap stays free of the ``imbue.mngr_imbue_cloud``
    import (which transitively pulls in mngr).
    """
    profile_dir = read_active_profile_dir(mngr_host_dir_for(root_name))
    if profile_dir is None:
        return None
    return profile_dir / "providers" / "imbue_cloud" / "sessions" / "accounts.json"


_IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"

# Runtime knobs written into each per-account ``[providers.imbue_cloud_<slug>]``
# block so the imbue_cloud slow (rebuild) path runs the agent container under
# gVisor with the runsc hardening args. These mirror the forever-claude-template
# ``[providers.ovh]`` bake settings; ``ImbueCloudProviderConfig`` (which extends
# ``VpsProviderConfig``) forwards them onto the delegated vps_docker
# provider, and ``install_gvisor_runtime`` also drives the slow path's SSH
# host-setup so a leased host that lacks runsc has it installed before the
# container is rebuilt under it.
_IMBUE_CLOUD_DOCKER_RUNTIME: Final[str] = "runsc"
_IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME: Final[bool] = True
_IMBUE_CLOUD_DEFAULT_START_ARGS: Final[tuple[str, ...]] = ("--workdir=/", "--security-opt=no-new-privileges")

# Backend name + container-hardening knobs written into each per-region
# ``[providers.aws-<region>]`` block. The AWS provider is region-locked per
# instance (EC2's API is per-region), so minds writes one block per
# ``CONFIGURED_AWS_REGIONS`` entry and the create address selects the right one.
# The gVisor/runsc settings mirror the forever-claude-template ``[providers.ovh]``
# / ``[providers.vultr]`` bake settings so the EC2 outer host runs the agent in a
# runsc-hardened container; the matching ``docker run`` start args live in the
# template ``[create_templates.aws]``.
_AWS_BACKEND_NAME: Final[str] = "aws"
_AWS_DOCKER_RUNTIME: Final[str] = "runsc"
_AWS_INSTALL_GVISOR_RUNTIME: Final[bool] = True
_AWS_PROVIDER_NAME_PREFIX: Final[str] = "aws-"
# EC2 instance size for minds AWS workspaces. The mngr_aws default (t3.small,
# 2 GB) is too small for the full forever-claude-template build (uv sync + npm
# ci/build OOMs/thrashes on 2 GB); minds workspaces default to t3.large (8 GB).
_AWS_DEFAULT_INSTANCE_TYPE: Final[str] = "t3.large"


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
    settings_dir = read_active_profile_dir(mngr_host_dir_for(root_name))
    if settings_dir is None or not settings_dir.exists():
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
    connector_url: str,
    root_name: str | None = None,
    force_enable: bool = True,
) -> bool:
    """Register ``[providers.imbue_cloud_<slug>]`` in mngr's settings.toml.

    Called by minds when a SuperTokens session for ``email`` is created
    (signin/signup/oauth-success) and from the bootstrap reconcile. Idempotent:
    a no-op if an equivalent entry already exists.

    ``connector_url`` is the URL of the ``remote_service_connector`` the
    provider should talk to. It is written into the provider block as
    ``connector_url`` so the ``mngr_imbue_cloud`` plugin no longer needs a
    baked-in default. Callers (i.e. ``minds run``) source this from the
    loaded ``ClientEnvConfig``.

    When ``force_enable`` is True (signin events), ``is_enabled`` is set to
    True even if the block was previously disabled (e.g. via the providers
    panel's Disable button). When False (bootstrap reconcile on a returning
    user), any pre-existing ``is_enabled`` value is preserved so an account
    the user previously disabled stays disabled until they sign in again.

    Returns ``True`` when the file was modified, so callers know whether
    to bounce ``mngr observe`` (the running process needs a restart to
    see the new provider instance).

    Always (re-)runs :func:`_ensure_mngr_settings` before touching the
    per-account block. ``apply_bootstrap`` calls ``_ensure_mngr_settings``
    at minds-startup, but for a freshly-created ``MINDS_ROOT_NAME`` the
    mngr profile dir doesn't exist yet at that point, so the call
    silently no-ops. By the time a signin fires this function, mngr has
    been initialized (the in-process ``mngr forward`` subprocess does
    that), so the second call lands the suppression block + recursive-
    disable that the first call missed. Without this, the auto-created
    default ``[providers.imbue_cloud]`` instance trips every
    ``mngr observe`` cycle with ``MissingConnectorUrlError`` and the
    first ``mngr create`` against this env fails outright.
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    _ensure_mngr_settings(root_name)
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
        and existing.get("connector_url") == connector_url
        and existing_is_enabled == desired_is_enabled
        and existing.get("docker_runtime") == _IMBUE_CLOUD_DOCKER_RUNTIME
        and existing.get("install_gvisor_runtime") == _IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME
        and existing.get("default_start_args") == list(_IMBUE_CLOUD_DEFAULT_START_ARGS)
    ):
        return False
    new_block = tomlkit.table()
    new_block["backend"] = _IMBUE_CLOUD_BACKEND_NAME
    new_block["account"] = email
    new_block["connector_url"] = connector_url
    if desired_is_enabled is not None:
        new_block["is_enabled"] = desired_is_enabled
    # Run the rebuilt agent container under gVisor with the runsc hardening args
    # (see the module constants above).
    new_block["docker_runtime"] = _IMBUE_CLOUD_DOCKER_RUNTIME
    new_block["install_gvisor_runtime"] = _IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME
    new_block["default_start_args"] = list(_IMBUE_CLOUD_DEFAULT_START_ARGS)
    providers[provider_name] = new_block
    _atomic_write_settings(settings_path, doc)
    logger.info("imbue_cloud provider {} registered in {}", provider_name, settings_path)
    return True


def is_imbue_cloud_provider_enabled_for_account(email: str, *, root_name: str | None = None) -> bool:
    """Return whether ``[providers.imbue_cloud_<slug>]`` is currently enabled.

    Reads the ``is_enabled`` field from the active mngr settings.toml so
    the desktop UI can render "Signed out" on a chip whose provider the
    user disabled via the providers panel. Treats a missing entry or a
    missing ``is_enabled`` field as enabled (per mngr's default), and
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


def list_disabled_provider_names(*, root_name: str | None = None) -> list[str]:
    """Return provider names that minds' active settings file marks ``is_enabled = false``.

    Used by the providers panel to enumerate the disabled set (which discovery
    skips and so are absent from the FullDiscoverySnapshotEvent). Reads only
    minds' active settings file -- providers defined only in mngr's own
    settings.toml with is_enabled=false are not surfaced here. Returns an
    empty list when the file does not exist yet (fresh install).
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    settings_path = _resolve_active_settings_path(root_name)
    if settings_path is None or not settings_path.exists():
        return []
    parsed = tomllib.loads(settings_path.read_text())
    providers = parsed.get("providers")
    if not isinstance(providers, dict):
        return []
    disabled: list[str] = []
    for name, block in providers.items():
        if isinstance(block, dict) and block.get("is_enabled") is False:
            disabled.append(name)
    return sorted(disabled)


def set_provider_is_enabled(provider_name: str, is_enabled: bool, *, root_name: str | None = None) -> bool:
    """Set ``is_enabled`` for the named provider in minds' active settings file.

    Generic over any provider name -- used by minds' providers panel toggle to
    let the user disable an errored provider (silencing its noise) or re-enable
    a previously-disabled one. Always writes to minds' active settings file.
    If ``[providers.<provider_name>]`` does not exist there, creates it with
    just ``is_enabled = <is_enabled>`` as an override on top of mngr's merged
    config. Enable writes ``is_enabled = true`` explicitly (symmetric with
    Disable).

    Idempotent: returns ``True`` only when the file was actually modified.
    Returns ``False`` (and does nothing) when the minds root is not yet set up
    (no active settings file path can be resolved).
    """
    if root_name is None:
        root_name = resolve_minds_root_name()
    settings_path = _resolve_active_settings_path(root_name)
    if settings_path is None:
        return False
    if settings_path.exists():
        doc = tomlkit.loads(settings_path.read_text())
    else:
        doc = tomlkit.document()
    providers = doc.get("providers")
    if not isinstance(providers, dict):
        providers = tomlkit.table()
        doc["providers"] = providers
    existing = providers.get(provider_name)
    if not isinstance(existing, dict):
        # Block doesn't exist yet -- create it with just is_enabled.
        new_block = tomlkit.table()
        new_block["is_enabled"] = is_enabled
        providers[provider_name] = new_block
        _atomic_write_settings(settings_path, doc)
        logger.info("Created provider block for {} with is_enabled={} in {}", provider_name, is_enabled, settings_path)
        return True
    if existing.get("is_enabled") == is_enabled:
        return False
    existing["is_enabled"] = is_enabled
    _atomic_write_settings(settings_path, doc)
    logger.info("Set provider {} is_enabled={} in {}", provider_name, is_enabled, settings_path)
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
