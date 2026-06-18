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
  ``{plugin_data_dir}/hosts/{host_id}/latchkey_permissions.json``
  (plus the deny-all default and the admin file at the data-dir root)
  so every agent running on the same host shares one permissions file.
  Only the subset of detent's file schema that we actually produce is
  modeled. :func:`save_permissions` is used by the three pre-gateway
  bootstrap paths (default file, admin file, per-agent opaque
  baseline); reads and per-host edits go through the gateway's
  ``permissions`` extension instead.

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

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.primitives import HostId

# Sub-directory under the user's ``latchkey_directory`` that holds every
# file written by this plugin (gateway record, default permissions,
# per-agent permissions, opaque handles, log files). Kept in a separate
# subtree so the plugin's files cannot collide with anything the
# upstream ``latchkey`` CLI writes under ``LATCHKEY_DIRECTORY``.
PLUGIN_DATA_SUBDIR_NAME: Final[str] = "mngr_latchkey"

_FORWARD_RECORD_FILENAME: Final[str] = "latchkey_forward.json"
_FORWARD_LOG_FILENAME: Final[str] = "latchkey_forward.log"
# The forward supervisor's structured log must be named exactly ``events.jsonl``
# so the standard mngr JSONL sink prunes its rotated copies
# (``events.jsonl.<rotation_timestamp>``), whose cleanup pattern is hard-coded to
# that name. It lives directly in the plugin data dir (no extra subdirectory).
_EVENTS_LOG_FILENAME: Final[str] = "events.jsonl"
_DEFAULT_PERMISSIONS_FILENAME: Final[str] = "latchkey_default_permissions.json"
_ADMIN_PERMISSIONS_FILENAME: Final[str] = "latchkey_admin_permissions.json"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"
_HOSTS_DIR_NAME: Final[str] = "hosts"
_OPAQUE_PERMISSIONS_DIR_NAME: Final[str] = "permissions"


def plugin_data_dir(latchkey_directory: Path) -> Path:
    """Return ``<latchkey_directory>/mngr_latchkey/``.

    Centralized so callers don't have to know the subdir name; pair with
    :attr:`Latchkey.plugin_data_dir` for the in-class accessor.
    """
    return latchkey_directory / PLUGIN_DATA_SUBDIR_NAME


# -- Forward supervisor info ---------------------------------------------------


class LatchkeyForwardInfo(FrozenModel):
    """Metadata identifying a running detached ``mngr latchkey forward`` supervisor.

    ``gateway_port`` is initially ``None`` (the embedder writes the
    record before the supervisor has finished starting up) and gets
    populated by the supervisor itself once it has spawned and
    port-bound the shared ``latchkey gateway`` subprocess. Consumers
    that want to talk to the gateway must poll for a non-``None``
    value.
    """

    pid: int = Field(description="PID of the ``mngr latchkey forward`` process")
    started_at: datetime = Field(description="UTC timestamp when the supervisor was started")
    gateway_port: int | None = Field(
        default=None,
        description=(
            "TCP port the shared ``latchkey gateway`` subprocess is listening on, or ``None`` "
            "while the gateway is still coming up. Pair with ``listen_host`` (always 127.0.0.1 "
            "today) to form the gateway URL. Consumers also need the password, which is "
            "deterministically derived from the user's latchkey encryption key via "
            ":meth:`Latchkey.derive_gateway_password` -- it is intentionally NOT stored on disk."
        ),
    )


def forward_info_path(data_dir: Path) -> Path:
    """Return the path to the forward supervisor info record."""
    return data_dir / _FORWARD_RECORD_FILENAME


def save_forward_info(data_dir: Path, info: LatchkeyForwardInfo) -> None:
    """Write the forward supervisor info record, overwriting any existing one."""
    path = forward_info_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(info.model_dump_json(indent=2))
    logger.debug("Saved mngr latchkey forward info at {}", path)


def update_forward_info_gateway_port(data_dir: Path, gateway_port: int) -> None:
    """Stamp the bound gateway port onto the existing forward record.

    Called by the ``mngr latchkey forward`` subcommand immediately
    after its child :class:`Latchkey` reports a successfully-bound
    gateway port. The record is preserved verbatim except for the
    ``gateway_port`` field so the embedder's view of the supervisor
    PID / started_at is not silently overwritten.
    """
    existing = load_forward_info(data_dir)
    if existing is None:
        raise LatchkeyStoreError(
            f"No forward info record at {forward_info_path(data_dir)} to stamp gateway_port={gateway_port} onto; "
            "refusing to silently leave the gateway invisible to consumers.",
        )
    save_forward_info(
        data_dir,
        existing.model_copy_update(to_update(existing.field_ref().gateway_port, gateway_port)),
    )


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
    """Return the raw stdout/stderr capture-log path for the detached ``mngr latchkey forward`` subprocess.

    This file holds whatever the detached process writes to its real
    stdout/stderr file descriptors (human-format console log lines, plus any
    pre-logging tracebacks or Click error messages). Its fd is handed straight
    to the subprocess, so it cannot be rotated mid-write and is intentionally
    left unrotated. For the process's own structured, timestamped, in-run-rotated
    log see :func:`forward_events_log_path`.
    """
    return data_dir / _FORWARD_LOG_FILENAME


def forward_events_log_path(data_dir: Path) -> Path:
    """Return the structured JSONL log path for the detached ``mngr latchkey forward`` process.

    Companion to :func:`forward_log_path`: where that file captures the raw
    stdout/stderr of the detached process, this one receives the process's own
    structured loguru events (nanosecond timestamps, level, message, ...) -- and
    the shared ``latchkey gateway`` subprocess's output, which the supervisor
    routes through loguru. It is the standard mngr JSONL log, size-rotated by
    :func:`imbue.imbue_common.logging.make_jsonl_file_sink` (rotated copies
    pruned), and the forward process is pointed at it via ``--log-file`` so its
    structured log is co-located with the rest of the plugin's files instead of
    mixed into the shared host-dir events stream.
    """
    return data_dir / _EVENTS_LOG_FILENAME


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


class LatchkeyPermissionsConfig(FrozenModel):
    """In-memory representation of a Latchkey/Detent permissions config file.

    Models only the subset of detent's config schema that minds itself
    produces: the top-level ``rules`` and ``schemas`` sections, with
    every rule in the plain ``{scope: [permission, ...]}`` shape (the
    ``{"schemas": [...], "hooks": [...]}`` rule-value form is not used
    by minds and is not modeled). Detent's ``include`` directive is
    not modeled either; ``extra="ignore"`` makes Pydantic silently drop
    any such hand-edited keys on load, so they disappear on the next
    minds-driven save.
    """

    # Override FrozenModel's ``extra="forbid"`` so hand-edited fields
    # detent accepts but minds does not produce (notably ``include``)
    # don't make ``load_permissions`` fail; they're dropped on the next
    # save instead, matching the previous hand-rolled loader's behavior.
    model_config = ConfigDict(extra="ignore")

    rules: tuple[dict[str, list[str]], ...] = Field(
        default_factory=tuple,
        description="Ordered rules. Each rule is one scope schema -> list of permission schemas.",
    )
    schemas: dict[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            "Optional inline detent request-schema definitions, keyed by schema name. "
            "Used by the per-agent baseline to grant access to specific gateway-self endpoints "
            "without depending on names from detent's built-in schema catalog."
        ),
    )


def hosts_dir(data_dir: Path) -> Path:
    """Return the directory under ``data_dir`` that holds every per-host subdirectory."""
    return data_dir / _HOSTS_DIR_NAME


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


def admin_permissions_path(data_dir: Path) -> Path:
    """Return the path to the admin permissions file used by the management UI.

    Holds a single ``{\"any\": [\"any\"]}`` rule -- a wildcard grant that
    lets the admin client reach every service and manage every host's permissions.
    """
    return data_dir / _ADMIN_PERMISSIONS_FILENAME


def ensure_admin_permissions_file(data_dir: Path) -> Path:
    """Materialize the admin permissions file if missing and return its path.

    Idempotent: a pre-existing file is left untouched so that any
    hand-edited overrides survive across restarts. The newly-created
    file always holds the wildcard ``{\"any\": [\"any\"]}`` rule.
    """
    path = admin_permissions_path(data_dir)
    if path.is_file():
        return path
    admin_config = LatchkeyPermissionsConfig(rules=({"any": ["any"]},))
    save_permissions(path, admin_config)
    return path


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
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to link opaque permissions handle {opaque_path} to {host_path}: {e}") from e
    point_opaque_handle_at_host(data_dir, opaque_path, host_id)
    logger.debug("Linked opaque latchkey permissions handle {} -> {}", opaque_path, host_path)


def point_opaque_handle_at_host(
    data_dir: Path,
    opaque_path: Path,
    host_id: HostId,
) -> None:
    """(Re)create ``opaque_path`` as a symlink to the host's canonical permissions file.

    Unlike :func:`link_opaque_permissions_to_host`, this moves nothing: it
    only ensures the opaque handle is a symlink pointing at
    ``permissions_path_for_host``. Use it when the canonical file already
    exists (or was materialized directly) and the handle needs to point at
    it -- e.g. when recovering an agent whose opaque handle went missing.

    Idempotent: an existing handle (regular file, wrong-target symlink, or
    already-correct symlink) is atomically replaced by a fresh symlink. The
    target is *absolute* so moving the symlink later doesn't break it.

    Raises ``LatchkeyStoreError`` if the symlink cannot be (re)created.
    """
    host_path = permissions_path_for_host(data_dir, host_id)
    try:
        opaque_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_target = host_path.resolve()
        # ``os.replace`` requires both paths on the same filesystem, which is
        # guaranteed here: the temp link and the handle both live under
        # ``data_dir``.
        tmp_link = opaque_path.with_name(opaque_path.name + ".linktmp")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(absolute_target)
        os.replace(tmp_link, opaque_path)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to point opaque permissions handle {opaque_path} at {host_path}: {e}") from e
    logger.debug("Pointed opaque latchkey permissions handle {} -> {}", opaque_path, host_path)


def save_permissions(path: Path, config: LatchkeyPermissionsConfig) -> None:
    """Atomically write the permissions config to disk with mode 0o600.

    Used by the pre-gateway-startup write paths (deny-all default,
    admin file, per-agent opaque baseline) and by the host-allowed-agent
    editor (:func:`imbue.mngr_latchkey.agent_setup.register_agent_for_host`).
    User-driven per-service grants still go through the gateway's
    ``permissions`` extension instead.

    An empty ``schemas`` dict is omitted from the output (detent
    accepts both shapes); ``rules`` is always emitted, even when empty.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pydantic's ``exclude=`` drops the field entirely; we drop
    # ``schemas`` when empty so existing on-disk files (and the
    # gateway's own writers) keep emitting the same ``{"rules": ...}``
    # shape they always have.
    exclude: set[str] = set() if config.schemas else {"schemas"}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(config.model_dump_json(indent=2, exclude=exclude))
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)
    logger.debug("Wrote permissions config to {} ({} rule(s))", path, len(config.rules))


def load_permissions(path: Path) -> LatchkeyPermissionsConfig:
    """Read a permissions config from disk.

    Used by the host-allowed-agent editor (and tests) to read + extend
    an existing permissions file. The reverse of :func:`save_permissions`:
    parses the JSON file via Pydantic, which enforces the documented
    shape (``rules`` is a list of ``{scope: [perm, ...]}`` objects,
    ``schemas`` is an object of JSON values) and silently drops any
    other top-level keys (e.g. detent's ``include``) per the model's
    ``extra="ignore"`` config.

    Raises:
        LatchkeyStoreError: if the file is missing, unreadable, not
            valid JSON, or doesn't match the documented schema.
    """
    if not path.is_file():
        raise LatchkeyStoreError(f"Permissions file does not exist: {path}")
    try:
        raw = path.read_text()
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to read permissions file {path}: {e}") from e
    try:
        return LatchkeyPermissionsConfig.model_validate_json(raw)
    except ValidationError as e:
        raise LatchkeyStoreError(f"Permissions file {path} is malformed: {e}") from e
