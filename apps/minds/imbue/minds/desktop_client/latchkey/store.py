"""On-disk persistence for the latchkey package.

Two kinds of per-agent files live here:

* ``LatchkeyGatewayInfo`` -- metadata identifying the running ``latchkey
  gateway`` subprocess for an agent (host, port, pid, started_at). Used so
  the next desktop-client launch can adopt or drop existing gateways.
* ``PermissionsConfig`` -- the contents of latchkey's permissions config
  file for an agent, in detent's rule format. Stored on disk as
  ``latchkey_permissions.json``. Latchkey reads this file at every
  request via ``LATCHKEY_PERMISSIONS_CONFIG``; minds rewrites it
  whenever the user grants or revokes permissions.

Both share the ``{data_dir}/agents/{agent_id}/...`` layout and the same
atomic-write pattern (write to ``.tmp``, chmod, rename).
"""

import json
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import AgentId

_GATEWAY_RECORD_FILENAME: Final[str] = "latchkey_gateway.json"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"
_AGENTS_DIR_NAME: Final[str] = "agents"


# -- Gateway info --------------------------------------------------------------


class LatchkeyGatewayInfo(FrozenModel):
    """Metadata identifying a running Latchkey gateway subprocess.

    Used both as the return type of manager methods and as the on-disk
    representation (one file per agent).
    """

    agent_id: AgentId = Field(description="The agent this gateway is dedicated to")
    host: str = Field(description="Host the gateway is listening on (typically 127.0.0.1)")
    port: int = Field(description="Port the gateway is listening on")
    pid: int = Field(description="PID of the ``latchkey gateway`` process")
    started_at: datetime = Field(description="UTC timestamp when the gateway was started")


def _gateway_info_path(data_dir: Path, agent_id: AgentId) -> Path:
    return data_dir / _AGENTS_DIR_NAME / str(agent_id) / _GATEWAY_RECORD_FILENAME


def save_gateway_info(data_dir: Path, info: LatchkeyGatewayInfo) -> None:
    """Write a gateway info record for an agent, overwriting any existing one."""
    path = _gateway_info_path(data_dir, info.agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(info.model_dump_json(indent=2))
    logger.debug("Saved latchkey gateway info for agent {} at {}", info.agent_id, path)


def load_gateway_info(data_dir: Path, agent_id: AgentId) -> LatchkeyGatewayInfo | None:
    """Read the gateway info for an agent, or None if missing or malformed."""
    path = _gateway_info_path(data_dir, agent_id)
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


def delete_gateway_info(data_dir: Path, agent_id: AgentId) -> None:
    """Remove the stored gateway info for an agent (no-op if absent)."""
    path = _gateway_info_path(data_dir, agent_id)
    if path.is_file():
        try:
            path.unlink()
            logger.debug("Deleted latchkey gateway info for agent {}", agent_id)
        except OSError as e:
            logger.warning("Failed to delete latchkey gateway info at {}: {}", path, e)


def list_gateway_infos(data_dir: Path) -> list[LatchkeyGatewayInfo]:
    """Return all persisted gateway infos under ``data_dir``.

    Malformed records are logged and skipped rather than aborting the scan.
    """
    agents_dir = data_dir / _AGENTS_DIR_NAME
    if not agents_dir.is_dir():
        return []
    infos: list[LatchkeyGatewayInfo] = []
    for entry in agents_dir.iterdir():
        if not entry.is_dir():
            continue
        path = entry / _GATEWAY_RECORD_FILENAME
        if not path.is_file():
            continue
        try:
            info = LatchkeyGatewayInfo.model_validate_json(path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Skipping malformed latchkey gateway info at {}: {}", path, e)
            continue
        infos.append(info)
    return infos


def gateway_log_path(data_dir: Path, agent_id: AgentId) -> Path:
    """Return the log file path for an agent's gateway subprocess."""
    return data_dir / _AGENTS_DIR_NAME / str(agent_id) / "latchkey_gateway.log"


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


class PermissionsConfig(FrozenModel):
    """In-memory representation of a Latchkey/Detent permissions config file.

    ``rules`` is the only field minds actively edits. ``schemas`` and
    ``include`` are preserved verbatim on round-trip so users can still
    hand-edit advanced configs without minds clobbering them.
    """

    # Each rule is a single-key dict mapping a scope schema name to a list
    # of permission schema names. We keep the wide ``Any`` value type for
    # the schemas dict because schema bodies are arbitrary JSON Schema
    # fragments that minds never inspects.
    rules: tuple[dict[str, list[str]], ...] = Field(
        default_factory=tuple,
        description="Ordered rules. Each rule is one scope schema -> list of permission schemas.",
    )
    schemas: dict[str, Any] | None = Field(
        default=None,
        description="Optional user-defined schemas, preserved verbatim.",
    )
    include: tuple[str, ...] | None = Field(
        default=None,
        description="Optional list of additional permission config files to include, preserved verbatim.",
    )


def permissions_path_for_agent(data_dir: Path, agent_id: AgentId) -> Path:
    """Return the path to the per-agent permissions file."""
    return data_dir / _AGENTS_DIR_NAME / str(agent_id) / _PERMISSIONS_FILENAME


def load_permissions(path: Path) -> PermissionsConfig:
    """Load a permissions config from disk.

    Returns an empty config if the file is absent. Raises
    ``MalformedPermissionsConfigError`` if the file exists but cannot be
    parsed as the expected shape.
    """
    if not path.is_file():
        return PermissionsConfig()

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

    schemas = data.get("schemas")
    if schemas is not None and not isinstance(schemas, dict):
        raise MalformedPermissionsConfigError(f"Expected 'schemas' to be an object in {path}")

    include = data.get("include")
    include_tuple: tuple[str, ...] | None
    if include is None:
        include_tuple = None
    elif isinstance(include, list) and all(isinstance(p, str) for p in include):
        include_tuple = tuple(include)
    else:
        raise MalformedPermissionsConfigError(f"Expected 'include' to be a list of strings in {path}")

    return PermissionsConfig(
        rules=tuple(rules),
        schemas=schemas,
        include=include_tuple,
    )


def save_permissions(path: Path, config: PermissionsConfig) -> None:
    """Atomically write the permissions config to disk with mode 0o600."""
    path.parent.mkdir(parents=True, exist_ok=True)

    serialized: dict[str, Any] = {"rules": [dict(rule) for rule in config.rules]}
    if config.schemas is not None:
        serialized["schemas"] = config.schemas
    if config.include is not None:
        serialized["include"] = list(config.include)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(serialized, indent=2))
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)
    logger.debug("Wrote permissions config to {} ({} rule(s))", path, len(config.rules))


def granted_permissions_for_service(
    config: PermissionsConfig,
    scope_schemas: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Return the currently-granted permissions for each of the given scope schemas.

    A scope that is absent from any rule maps to an empty tuple.
    """
    scope_set = set(scope_schemas)
    result: dict[str, tuple[str, ...]] = {scope: () for scope in scope_schemas}
    for rule in config.rules:
        for scope, permissions in rule.items():
            if scope in scope_set:
                result[scope] = tuple(permissions)
    return result


def set_permissions_for_service(
    config: PermissionsConfig,
    scope_schemas: Sequence[str],
    granted_permissions: Sequence[str],
) -> PermissionsConfig:
    """Return a new config with one rule per scope schema mapping to ``granted_permissions``.

    Replaces any existing rules whose key is one of ``scope_schemas`` and
    appends new rules for any scopes that didn't have a rule yet. Rules
    for unrelated scopes are preserved in their original order.
    """
    if not scope_schemas:
        raise LatchkeyStoreError("scope_schemas must be non-empty")
    if not granted_permissions:
        raise LatchkeyStoreError(
            "granted_permissions must be non-empty; the UI must block empty grants",
        )

    permissions_list = list(granted_permissions)
    scope_set = set(scope_schemas)
    seen_scopes: set[str] = set()
    new_rules: list[dict[str, list[str]]] = []

    # Keep unrelated rules in place; replace rules whose scope is being managed.
    for rule in config.rules:
        if len(rule) == 1:
            scope = next(iter(rule.keys()))
            if scope in scope_set:
                if scope in seen_scopes:
                    continue
                new_rules.append({scope: list(permissions_list)})
                seen_scopes.add(scope)
                continue
        new_rules.append({scope: list(permissions) for scope, permissions in rule.items()})

    # Append rules for any scopes that didn't appear in the existing config.
    for scope in scope_schemas:
        if scope not in seen_scopes:
            new_rules.append({scope: list(permissions_list)})
            seen_scopes.add(scope)

    return config.model_copy_update(
        to_update(config.field_ref().rules, tuple(new_rules)),
    )
