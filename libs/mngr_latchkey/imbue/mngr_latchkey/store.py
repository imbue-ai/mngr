"""On-disk persistence for the latchkey package.

Everything the plugin writes lives under ``<latchkey_directory>/mngr_latchkey/``
(``Latchkey.plugin_data_dir``), keeping plugin metadata cleanly
segregated from upstream latchkey's own ``LATCHKEY_DIRECTORY`` files
while sharing a single root path the user has to remember.

Two kinds of files live there:

* ``LatchkeyGatewayInfo`` -- metadata identifying the single shared
  ``latchkey gateway`` subprocess (host, port, pid, started_at). Used
  so the next launch can adopt or drop the existing gateway. Stored
  at ``{plugin_data_dir}/latchkey_gateway.json``.
* ``LatchkeyPermissionsConfig`` -- the contents of latchkey's permissions
  config for a host, in detent's rule format. Stored on disk per-host
  as ``{plugin_data_dir}/hosts/{host_name}/latchkey_permissions.json``.
  The shared gateway consults this file via the
  ``X-Latchkey-Gateway-Permissions-Override`` header injected through
  the JWT minted at agent-creation time. Rewritten whenever the user
  grants or revokes permissions. Only the subset of detent's file
  schema that we actually produce is modeled.

The ``host_name`` is known up-front (minds derives it from the agent
name), so the JWT can be minted directly for the canonical host path
before ``mngr create`` runs -- no per-agent opaque indirection is
needed. Once ``mngr create`` returns the canonical ``host_id`` we
verify it against the ``{plugin_data_dir}/hosts/{host_name}/host-id``
file: a mismatch means the host with that name has been recreated
(stale prior permissions), so we clear the permissions file and
overwrite ``host-id`` with the freshly-reported value.

All writes share the same atomic-write pattern (write to ``.tmp``,
chmod, rename) where applicable.

For every helper here the parameter name ``plugin_data_dir`` refers to
the ``mngr_latchkey/`` subdir under the user's latchkey directory; it
is what :attr:`Latchkey.plugin_data_dir` returns.
"""

import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName

# Sub-directory under the user's ``latchkey_directory`` that holds every
# file written by this plugin (gateway record, default permissions,
# per-host permissions, log files). Kept in a separate subtree so the
# plugin's files cannot collide with anything the upstream ``latchkey``
# CLI writes under ``LATCHKEY_DIRECTORY``.
PLUGIN_DATA_SUBDIR_NAME: Final[str] = "mngr_latchkey"

_GATEWAY_RECORD_FILENAME: Final[str] = "latchkey_gateway.json"
_GATEWAY_LOG_FILENAME: Final[str] = "latchkey_gateway.log"
_DEFAULT_PERMISSIONS_FILENAME: Final[str] = "latchkey_default_permissions.json"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"
_HOSTS_DIR_NAME: Final[str] = "hosts"
_HOST_ID_FILENAME: Final[str] = "host-id"


def plugin_data_dir(latchkey_directory: Path) -> Path:
    """Return ``<latchkey_directory>/mngr_latchkey/``.

    Centralized so callers don't have to know the subdir name; pair with
    :attr:`Latchkey.plugin_data_dir` for the in-class accessor.
    """
    return latchkey_directory / PLUGIN_DATA_SUBDIR_NAME


# -- Gateway info --------------------------------------------------------------


class LatchkeyGatewayInfo(FrozenModel):
    """Metadata identifying the running shared Latchkey gateway subprocess."""

    host: str = Field(description="Host the gateway is listening on (typically 127.0.0.1)")
    port: int = Field(description="Port the gateway is listening on")
    pid: int = Field(description="PID of the ``latchkey gateway`` process")
    started_at: datetime = Field(description="UTC timestamp when the gateway was started")


def gateway_info_path(data_dir: Path) -> Path:
    """Return the path to the shared gateway info record."""
    return data_dir / _GATEWAY_RECORD_FILENAME


def save_gateway_info(data_dir: Path, info: LatchkeyGatewayInfo) -> None:
    """Write the gateway info record, overwriting any existing one."""
    path = gateway_info_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(info.model_dump_json(indent=2))
    logger.debug("Saved latchkey gateway info at {}", path)


def load_gateway_info(data_dir: Path) -> LatchkeyGatewayInfo | None:
    """Read the gateway info, or None if missing or malformed."""
    path = gateway_info_path(data_dir)
    if not path.is_file():
        return None
    try:
        raw = path.read_text()
    except OSError as e:
        logger.warning("Failed to read latchkey gateway info at {}: {}", path, e)
        return None
    try:
        return LatchkeyGatewayInfo.model_validate_json(raw)
    except ValueError as e:
        logger.warning("Malformed latchkey gateway info at {}: {}", path, e)
        return None


def delete_gateway_info(data_dir: Path) -> None:
    """Remove the stored gateway info (no-op if absent)."""
    path = gateway_info_path(data_dir)
    if path.is_file():
        try:
            path.unlink()
            logger.debug("Deleted latchkey gateway info at {}", path)
        except OSError as e:
            logger.warning("Failed to delete latchkey gateway info at {}: {}", path, e)


def gateway_log_path(data_dir: Path) -> Path:
    """Return the log file path for the shared gateway subprocess."""
    return data_dir / _GATEWAY_LOG_FILENAME


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


def host_data_dir(data_dir: Path, host_name: HostName) -> Path:
    """Return the per-host subdirectory under ``data_dir``.

    Each host keeps its own ``latchkey_permissions.json`` and ``host-id``
    file here. The host name is deterministic at create time (e.g.
    ``{agent_name}-host`` for minds-created hosts), so the JWT can
    reference this path up-front -- no per-agent opaque indirection is
    needed.
    """
    return data_dir / _HOSTS_DIR_NAME / str(host_name)


def permissions_path_for_host(data_dir: Path, host_name: HostName) -> Path:
    """Return the path to the per-host permissions file."""
    return host_data_dir(data_dir, host_name) / _PERMISSIONS_FILENAME


def host_id_path_for_host(data_dir: Path, host_name: HostName) -> Path:
    """Return the path to the per-host ``host-id`` file.

    The file holds the canonical ``HostId`` reported by ``mngr create``
    so we can detect when a host with the same name has been recreated
    (stale prior permissions) and clear permissions accordingly.
    """
    return host_data_dir(data_dir, host_name) / _HOST_ID_FILENAME


def read_stored_host_id(data_dir: Path, host_name: HostName) -> HostId | None:
    """Return the recorded ``HostId`` for ``host_name``, or ``None`` if absent.

    Treats an unreadable or malformed file as absent (logs a warning)
    rather than raising: the caller is the staleness check, and a
    corrupted file is itself a sign of staleness.
    """
    path = host_id_path_for_host(data_dir, host_name)
    if not path.is_file():
        return None
    try:
        raw = path.read_text().strip()
    except OSError as e:
        logger.warning("Failed to read host-id file at {}: {}", path, e)
        return None
    if not raw:
        return None
    try:
        return HostId(raw)
    except ValueError as e:
        logger.warning("Malformed host-id in {}: {}", path, e)
        return None


def write_stored_host_id(data_dir: Path, host_name: HostName, host_id: HostId) -> None:
    """Atomically write ``host_id`` to the per-host ``host-id`` file."""
    path = host_id_path_for_host(data_dir, host_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(str(host_id))
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)
    logger.debug("Wrote host-id {} to {}", host_id, path)


def default_permissions_path(data_dir: Path) -> Path:
    """Return the path to the shared gateway's default (deny-all) permissions file.

    The shared ``latchkey gateway`` consults this file when an incoming
    request does not carry a valid ``X-Latchkey-Gateway-Permissions-Override``
    JWT. Minds materializes it with empty rules (deny-all) so an agent
    that escapes the JWT mechanism cannot reach any service.
    """
    return data_dir / _DEFAULT_PERMISSIONS_FILENAME


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
