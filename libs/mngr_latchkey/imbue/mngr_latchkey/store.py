"""On-disk persistence for the latchkey package.

Everything the plugin writes lives under ``<latchkey_directory>/mngr_latchkey/``
(``Latchkey.plugin_data_dir``), keeping plugin metadata cleanly
segregated from upstream latchkey's own ``LATCHKEY_DIRECTORY`` files
while sharing a single root path the user has to remember.

Two kinds of state live there:

* ``LatchkeyForwardInfo`` -- metadata identifying the detached
  ``mngr latchkey forward`` supervisor (pid, started_at). Used by
  :class:`LatchkeyForwardSupervisor` so the next caller can adopt or
  drop the existing supervisor. Stored at
  ``{plugin_data_dir}/latchkey_forward.json``.
* ``LatchkeyPermissionsConfig`` -- the contents of latchkey's permissions
  config, in detent's rule format. Stored on disk per-host as
  ``{plugin_data_dir}/hosts/{host_id}/latchkey_permissions.json`` so
  every agent running on the same host shares one permissions file.
  The shared gateway consults this file via the
  ``X-Latchkey-Gateway-Permissions-Override`` header injected through
  the JWT minted at agent-creation time. Rewritten whenever the user
  grants or revokes permissions. Only the subset of detent's file
  schema that we actually produce is modeled.

The gateway never reads the per-host file directly via its host-id
path. Instead, an opaque ``{plugin_data_dir}/permissions/<uuid>.json``
file is created at agent-creation time (with empty rules), the JWT is
minted for *that* path, and after ``mngr create`` returns the canonical
host id we replace the opaque file with a symlink pointing at the
canonical host-keyed path. This indirection lets us mint and inject the
JWT before the host id is known (no flaky post-create ``mngr
provision`` step) while keeping the canonical permissions file at the
host-id path that ``LatchkeyPermissionGrantHandler`` already writes to.

Both share the same atomic-write pattern (write to ``.tmp``, chmod,
rename) where applicable.

For every helper here the parameter name ``plugin_data_dir`` refers to
the ``mngr_latchkey/`` subdir under the user's latchkey directory; it
is what :attr:`Latchkey.plugin_data_dir` returns.
"""

import json
import os
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import HostId

# Sub-directory under the user's ``latchkey_directory`` that holds every
# file written by this plugin (gateway record, default permissions,
# per-agent permissions, opaque handles, log files). Kept in a separate
# subtree so the plugin's files cannot collide with anything the
# upstream ``latchkey`` CLI writes under ``LATCHKEY_DIRECTORY``.
PLUGIN_DATA_SUBDIR_NAME: Final[str] = "mngr_latchkey"

_GATEWAY_LOG_FILENAME: Final[str] = "latchkey_gateway.log"
_FORWARD_RECORD_FILENAME: Final[str] = "latchkey_forward.json"
_FORWARD_LOG_FILENAME: Final[str] = "latchkey_forward.log"
_DEFAULT_PERMISSIONS_FILENAME: Final[str] = "latchkey_default_permissions.json"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"
_HOSTS_DIR_NAME: Final[str] = "hosts"
_OPAQUE_PERMISSIONS_DIR_NAME: Final[str] = "permissions"


def plugin_data_dir(latchkey_directory: Path) -> Path:
    """Return ``<latchkey_directory>/mngr_latchkey/``.

    Centralized so callers don't have to know the subdir name; pair with
    :attr:`Latchkey.plugin_data_dir` for the in-class accessor.
    """
    return latchkey_directory / PLUGIN_DATA_SUBDIR_NAME


# -- Gateway info --------------------------------------------------------------


def gateway_log_path(data_dir: Path) -> Path:
    """Return the log file path for the shared gateway subprocess."""
    return data_dir / _GATEWAY_LOG_FILENAME


# -- Forward supervisor info ---------------------------------------------------


class LatchkeyForwardInfo(FrozenModel):
    """Metadata identifying a running detached ``mngr latchkey forward`` supervisor."""

    pid: int = Field(description="PID of the ``mngr latchkey forward`` process")
    started_at: datetime = Field(description="UTC timestamp when the supervisor was started")


def forward_info_path(data_dir: Path) -> Path:
    """Return the path to the forward supervisor info record."""
    return data_dir / _FORWARD_RECORD_FILENAME


def save_forward_info(data_dir: Path, info: LatchkeyForwardInfo) -> None:
    """Write the forward supervisor info record, overwriting any existing one."""
    path = forward_info_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(info.model_dump_json(indent=2))
    logger.debug("Saved mngr latchkey forward info at {}", path)


def load_forward_info(data_dir: Path) -> LatchkeyForwardInfo | None:
    """Read the forward supervisor info, or None if missing or malformed."""
    path = forward_info_path(data_dir)
    if not path.is_file():
        return None
    try:
        raw = path.read_text()
    except OSError as e:
        logger.warning("Failed to read mngr latchkey forward info at {}: {}", path, e)
        return None
    try:
        return LatchkeyForwardInfo.model_validate_json(raw)
    except ValueError as e:
        logger.warning("Malformed mngr latchkey forward info at {}: {}", path, e)
        return None


def delete_forward_info(data_dir: Path) -> None:
    """Remove the stored forward supervisor info (no-op if absent)."""
    path = forward_info_path(data_dir)
    if path.is_file():
        try:
            path.unlink()
            logger.debug("Deleted mngr latchkey forward info at {}", path)
        except OSError as e:
            logger.warning("Failed to delete mngr latchkey forward info at {}: {}", path, e)


def forward_log_path(data_dir: Path) -> Path:
    """Return the log file path for the detached ``mngr latchkey forward`` subprocess."""
    return data_dir / _FORWARD_LOG_FILENAME


def ensure_browser_log_path(data_dir: Path) -> Path:
    """Return the log file path for the one-shot ``latchkey ensure-browser`` subprocess.

    Not agent-scoped: ``ensure-browser`` is a minds-wide one-time setup
    step that configures a shared Playwright/Chromium browser for the
    latchkey credential directory, run at most once per minds session.
    """
    return data_dir / "latchkey_ensure_browser.log"


# -- Permissions config (latchkey_permissions.json) ---------------------------


class LatchkeyStoreError(Exception):
    """Base exception for permissions-config persistence failures."""


class MalformedPermissionsConfigError(LatchkeyStoreError, ValueError):
    """Raised when an existing ``latchkey_permissions.json`` cannot be parsed."""


class LatchkeyPermissionsConfig(FrozenModel):
    """In-memory representation of a Latchkey/Detent permissions config file.

    Minds manages this file programmatically, so we model only the subset
    of detent's full schema that we ever produce: the ordered ``rules``
    list. Detent's ``schemas`` and ``include`` directives are intentionally
    not modeled; any hand-edited entries for those keys are silently
    dropped on the next minds-driven save.
    """

    # Each rule is a single-key dict mapping a scope schema name to a list
    # of permission schema names. Detent's wider rule shape (multi-key
    # dicts) is not produced by minds; we tolerate them on read but
    # collapse them to single-key form on write via
    # ``set_permissions_for_scope``.
    rules: tuple[dict[str, list[str]], ...] = Field(
        default_factory=tuple,
        description="Ordered rules. Each rule is one scope schema -> list of permission schemas.",
    )


def permissions_path_for_host(data_dir: Path, host_id: HostId) -> Path:
    """Return the path to the per-host permissions file.

    Every agent on the same host shares one permissions file: latchkey
    access is granted at the host level so all agents minds spawns on a
    host inherit the same gateway credentials and the same permissions.
    """
    return data_dir / _HOSTS_DIR_NAME / str(host_id) / _PERMISSIONS_FILENAME


def default_permissions_path(data_dir: Path) -> Path:
    """Return the path to the shared gateway's default (deny-all) permissions file.

    The shared ``latchkey gateway`` consults this file when an incoming
    request does not carry a valid ``X-Latchkey-Gateway-Permissions-Override``
    JWT. Minds materializes it with empty rules (deny-all) so an agent
    that escapes the JWT mechanism cannot reach any service.
    """
    return data_dir / _DEFAULT_PERMISSIONS_FILENAME


def opaque_permissions_dir(data_dir: Path) -> Path:
    """Return the directory that holds opaque-named per-agent permissions handles.

    Each handle is a UUID-named file (or symlink) created at
    agent-creation time. The JWT minted for the handle path is what gets
    injected into the agent's environment, so the gateway only ever
    reads through this opaque indirection -- the canonical agent-id
    path lives behind the symlink, never directly referenced by the
    JWT.
    """
    return data_dir / _OPAQUE_PERMISSIONS_DIR_NAME


_OPAQUE_PERMISSIONS_PATH_MAX_ATTEMPTS: Final[int] = 16


def new_opaque_permissions_path(data_dir: Path) -> Path:
    """Return a fresh, unused opaque-named permissions handle path.

    The caller is responsible for materializing the file.

    UUIDv4 collisions are astronomically rare, but we bound the retry
    loop just in case the underlying directory has somehow been seeded
    with every UUID we ever generate (e.g. a misconfigured test) so the
    function cannot loop forever.
    """
    parent = opaque_permissions_dir(data_dir)
    parent.mkdir(parents=True, exist_ok=True)
    for _ in range(_OPAQUE_PERMISSIONS_PATH_MAX_ATTEMPTS):
        candidate = parent / f"{uuid.uuid4().hex}.json"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise LatchkeyStoreError(
        f"Could not allocate a fresh opaque permissions path under {parent} after "
        f"{_OPAQUE_PERMISSIONS_PATH_MAX_ATTEMPTS} attempts"
    )


def link_opaque_permissions_to_host(
    data_dir: Path,
    opaque_path: Path,
    host_id: HostId,
) -> None:
    """Replace ``opaque_path`` with a symlink to the host's canonical permissions file.

    Called once after ``mngr create`` returns the canonical host id.
    The opaque file was created with deny-all baseline rules at
    create time; this function moves those baseline rules into the
    canonical ``permissions_path_for_host`` location (or discards them
    if a previous incarnation of the same host already had a permissions
    file there) and replaces the opaque path with a symlink to the
    canonical path so the JWT minted for the opaque path keeps resolving.

    The symlink target is *absolute* so renaming or moving the symlink
    later doesn't break the redirection.

    Raises ``LatchkeyStoreError`` if the linking fails.
    """
    host_path = permissions_path_for_host(data_dir, host_id)
    host_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if host_path.is_file() and not host_path.is_symlink():
            # Re-use case: another agent on the same host already has a
            # permissions file with prior grants. Keep them and discard
            # the freshly-created baseline.
            opaque_path.unlink()
        else:
            # First creation for this host_id: promote the baseline to
            # the canonical location.
            os.replace(opaque_path, host_path)
        # Recreate ``opaque_path`` as a symlink. ``os.replace`` requires
        # both paths to exist on the same filesystem, but that is
        # guaranteed here: opaque_path and host_path both live under
        # ``data_dir``.
        absolute_target = host_path.resolve()
        tmp_link = opaque_path.with_name(opaque_path.name + ".linktmp")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(absolute_target)
        os.replace(tmp_link, opaque_path)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to link opaque permissions handle {opaque_path} to {host_path}: {e}") from e
    logger.debug("Linked opaque latchkey permissions handle {} -> {}", opaque_path, host_path)


def load_permissions(path: Path) -> LatchkeyPermissionsConfig:
    """Load a permissions config from disk.

    Returns an empty config if the file is absent. Raises
    ``MalformedPermissionsConfigError`` if the file exists but cannot be
    parsed as the expected shape.
    """
    if not path.is_file():
        return LatchkeyPermissionsConfig()

    try:
        raw = path.read_text()
    except OSError as e:
        raise LatchkeyStoreError(f"Cannot read permissions file at {path}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise MalformedPermissionsConfigError(f"Invalid JSON in permissions file at {path}: {e}") from e

    if not isinstance(data, dict):
        raise MalformedPermissionsConfigError(f"Expected a JSON object at the top of {path}")

    rules_raw = data.get("rules", [])
    if not isinstance(rules_raw, list):
        raise MalformedPermissionsConfigError(f"Expected 'rules' to be a list in {path}")
    rules: list[dict[str, list[str]]] = []
    for rule in rules_raw:
        if not isinstance(rule, dict):
            raise MalformedPermissionsConfigError(f"Expected each rule to be an object in {path}")
        normalized: dict[str, list[str]] = {}
        for scope_name, permissions in rule.items():
            if not isinstance(scope_name, str):
                raise MalformedPermissionsConfigError(f"Rule scope keys must be strings in {path}")
            if not isinstance(permissions, list) or not all(isinstance(p, str) for p in permissions):
                raise MalformedPermissionsConfigError(
                    f"Rule values must be lists of permission schema names in {path}"
                )
            normalized[scope_name] = [str(p) for p in permissions]
        rules.append(normalized)

    # ``schemas`` and ``include`` are intentionally not modeled: minds
    # manages this file programmatically and only reads / writes the
    # subset of detent's schema we actually produce (the ``rules``
    # list). Any hand-edited entries for those keys are silently dropped
    # on the next save.

    return LatchkeyPermissionsConfig(rules=tuple(rules))


def save_permissions(path: Path, config: LatchkeyPermissionsConfig) -> None:
    """Atomically write the permissions config to disk with mode 0o600."""
    path.parent.mkdir(parents=True, exist_ok=True)

    serialized = {"rules": [dict(rule) for rule in config.rules]}

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(serialized, indent=2))
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)
    logger.debug("Wrote permissions config to {} ({} rule(s))", path, len(config.rules))


def granted_permissions_for_scope(
    config: LatchkeyPermissionsConfig,
    scope: str,
) -> tuple[str, ...]:
    """Return the currently-granted permissions for a single scope.

    A scope that does not appear in any rule yields an empty tuple. If
    multiple rules name the same scope (minds itself never writes that),
    the last occurrence wins -- mirroring detent's first-match-wins
    evaluation against the rule list.
    """
    granted: tuple[str, ...] = ()
    for rule in config.rules:
        for rule_scope, permissions in rule.items():
            if rule_scope == scope:
                granted = tuple(permissions)
    return granted


def set_permissions_for_scope(
    config: LatchkeyPermissionsConfig,
    scope: str,
    granted_permissions: Sequence[str],
) -> LatchkeyPermissionsConfig:
    """Return a new config with the rule for ``scope`` set to ``granted_permissions``.

    If a single-key rule for ``scope`` already exists, it is replaced in
    place; otherwise a new rule is appended. Rules for unrelated scopes
    are preserved verbatim. Pre-existing duplicates (two rules naming
    the same scope -- minds never writes that, but a hand-edited file
    might) are collapsed into the single rule we write.

    Callers wanting to manage multiple scopes call this once per scope.
    """
    if not granted_permissions:
        raise LatchkeyStoreError(
            "granted_permissions must be non-empty; the UI must block empty grants",
        )

    new_rules: list[dict[str, list[str]]] = []
    is_replaced = False
    for rule in config.rules:
        if scope in rule:
            if not is_replaced:
                new_rules.append({scope: list(granted_permissions)})
                is_replaced = True
            # else: drop the duplicate.
        else:
            new_rules.append({k: list(v) for k, v in rule.items()})
    if not is_replaced:
        new_rules.append({scope: list(granted_permissions)})

    return config.model_copy_update(
        to_update(config.field_ref().rules, tuple(new_rules)),
    )
