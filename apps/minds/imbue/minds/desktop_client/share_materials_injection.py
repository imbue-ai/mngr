"""Manage a shared workspace's materials in the agent's secrets directory.

The share-gateway service inside the workspace watches
``data/.secrets/share.env`` (relay coordinates + relay token): the whole share
stack starts when it appears and stops when it is removed. The grants document
lives next to it at ``data/.secrets/share_grants.toml`` and is re-read by the
gateway on every request, so a grants update takes effect immediately without
restarting anything.

We also drop the owner's account email at ``data/.state/share/owner_email`` so
in-workspace services can learn who owns the workspace (the gateway never
reveals the owner's email in a per-request header). It exists only while the
workspace is shared, so its presence doubles as a "this workspace is shared"
signal; it is removed at unshare alongside the secrets.

All files are written via ``mngr exec`` through the shared warm-process
``MngrCaller``, base64-encoded in transit so arbitrary emails and tokens never
need shell quoting.
"""

import base64
import json
import threading
from typing import Final

from loguru import logger
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

_SHARE_ENV_FILE: Final[str] = "data/.secrets/share.env"
_SHARE_GRANTS_FILE: Final[str] = "data/.secrets/share_grants.toml"
# App-readable (non-secret) owner email, present only while the workspace is
# shared. Lives under data/.state (machine state) rather than data/.secrets so
# services can read it without touching the relay token beside share.env.
_SHARE_OWNER_EMAIL_FILE: Final[str] = "data/.state/share/owner_email"

_SHARE_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0


class ShareInjectionError(RuntimeError):
    """Raised when the share materials could not be written into the agent."""


class MachineSharingLockRegistry(MutableModel):
    """Per-machine locks serializing the desktop backend's sharing writes.

    Two concurrent sharing edits for one machine (the Share pane open from the
    titlebar and the workspace list, or two windows) would otherwise run their
    full-document replaces fully interleaved; the machine-sharing PUT/DELETE
    handlers hold this lock so one machine's edits serialize regardless of
    which pane or window they came from.
    """

    _lock_by_host_id: dict[str, threading.Lock] = PrivateAttr(default_factory=dict)
    _registry_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def get_lock(self, host_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._lock_by_host_id.get(host_id)
            if lock is None:
                lock = threading.Lock()
                self._lock_by_host_id[host_id] = lock
            return lock


def build_share_env_text(
    workspace_domain: str,
    relay_token: str,
    connector_url: str,
    broker_url: str,
    # The hosted web chrome's origin, allowed to embed the workspace and probe
    # its gateway /_health; empty leaves the chrome locked out (pre-web shape).
    chrome_origin: str,
) -> str:
    """Render share.env in the shape the workspace's share-gateway parses.

    Deliberately carries NO relay endpoint: the gateway fetches its current
    relay set from the connector's assignment endpoint (relay-token auth) and
    re-polls, so fleet changes never require re-injecting materials.
    """
    lines = [
        f"export SHARE_WORKSPACE_DOMAIN={workspace_domain}",
        f"export SHARE_RELAY_TOKEN={relay_token}",
        f"export SHARE_CONNECTOR_URL={connector_url}",
        f"export SHARE_BROKER_URL={broker_url}",
    ]
    if chrome_origin:
        lines.append(f"export SHARE_CHROME_ORIGIN={chrome_origin}")
    return "\n".join(lines) + "\n"


def _toml_string_array(values: list[str]) -> str:
    """Render a list of plain strings as a TOML array (json-style quoting is valid TOML here)."""
    quoted = ", ".join('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in values)
    return f"[{quoted}]"


def render_grants_toml(workspace_grants: dict[str, list[str]], service_grants: dict[str, dict[str, list[str]]]) -> str:
    """Render the grants document the workspace gateway evaluates.

    ``workspace_grants`` is ``{"emails": [...], "email_domains": [...]}``;
    ``service_grants`` maps service name to the same shape. Values are emitted
    as TOML string arrays (json-style quoting is valid TOML for plain strings).
    """
    lines = [
        "[workspace]",
        f"emails = {_toml_string_array(workspace_grants.get('emails', []))}",
        f"email_domains = {_toml_string_array(workspace_grants.get('email_domains', []))}",
    ]
    for service_name in sorted(service_grants):
        grants = service_grants[service_name]
        lines.append("")
        lines.append(f"[services.{_quote_toml_key(service_name)}]")
        lines.append(f"emails = {_toml_string_array(grants.get('emails', []))}")
        lines.append(f"email_domains = {_toml_string_array(grants.get('email_domains', []))}")
    return "\n".join(lines) + "\n"


def _quote_toml_key(key: str) -> str:
    if key.replace("-", "").replace("_", "").isalnum():
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_file_via_exec(agent_id: AgentId, relative_path: str, content: str, mngr_caller: MngrCaller) -> None:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    directory, _, filename = relative_path.rpartition("/")
    # The tmp name comes from mktemp, never a fixed `<path>.tmp`: two
    # concurrent writers sharing one tmp path interleave their bytes and the
    # loser's mv publishes a corrupted file. With a unique tmp per write the
    # final mv stays atomic for readers and the last writer wins whole.
    result = mngr_caller.call(
        [
            "exec",
            str(agent_id),
            f'mkdir -p {directory} && tmp="$(mktemp {directory}/.{filename}.XXXXXX)" '
            f"&& printf '%s' {encoded} | base64 -d > \"$tmp\" "
            f'&& mv "$tmp" {relative_path}',
        ],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ShareInjectionError(f"Failed to write {relative_path} into agent {agent_id}: {result.stderr.strip()}")


def inject_share_grants_into_agent(agent_id: AgentId, grants_toml_text: str, mngr_caller: MngrCaller) -> None:
    """Write (or replace) the grants document. Takes effect on the gateway's next request."""
    _write_file_via_exec(agent_id, _SHARE_GRANTS_FILE, grants_toml_text, mngr_caller)


def inject_share_materials_into_agent(agent_id: AgentId, share_env_text: str, mngr_caller: MngrCaller) -> None:
    """Write (or replace) share.env; the workspace's share-gateway brings the stack up."""
    _write_file_via_exec(agent_id, _SHARE_ENV_FILE, share_env_text, mngr_caller)


def inject_share_owner_email_into_agent(agent_id: AgentId, owner_email: str, mngr_caller: MngrCaller) -> None:
    """Write the owner-email file so shared-workspace services can learn the owner.

    Best-effort: this is a convenience artifact (services must tolerate its
    absence), so a failed write is logged but never fails the enable. An empty
    email is skipped rather than writing a misleading empty file.
    """
    if not owner_email:
        logger.debug("Skipping owner-email injection for agent {}: no owner email", agent_id)
        return
    try:
        _write_file_via_exec(agent_id, _SHARE_OWNER_EMAIL_FILE, owner_email, mngr_caller)
    except ShareInjectionError as exc:
        logger.warning("Failed to inject owner email into agent {}: {}", agent_id, exc)


_SHARE_GATEWAY_SERVICE_DIR: Final[str] = "system/services/share_gateway"


def has_share_gateway_in_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> bool:
    """Whether the workspace's template ships the share-gateway service.

    Workspaces created from a pre-share-gateway template (minds-v0.3.11 and
    older) have nothing watching ``share.env``, so injecting share materials
    into them can never bring a share up. Conservative on exec failure:
    reported as absent, so the caller refuses with the actionable
    update-your-workspace message rather than provisioning a share that cannot
    work (a retry after the transient failure clears is cheap).

    # CLEANUP: this probe (and its caller's guard) can be removed once no
    # supported workspaces predate the share gateway -- i.e. after the first
    # post-v0.3.11 release is deployed and the remaining old workspaces have
    # been updated via update-self (they cannot share until they update).
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"test -d {_SHARE_GATEWAY_SERVICE_DIR}", "--no-start"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def has_share_materials_in_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> bool:
    """Whether share.env is present inside the agent (the share stack's on-switch).

    Distinguishes an actively-shared workspace from one whose earlier enable
    failed between the connector-side create and the injection. Conservative on
    exec failure: reported as absent, so the caller re-provisions -- which is
    safe, because the connector reuses the share row and the injection
    overwrites in place.
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"test -f {_SHARE_ENV_FILE}", "--no-start"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def clear_share_materials_from_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> None:
    """Remove share.env + the grants file + the owner-email file; the share-gateway tears the stack down.

    Best-effort: a failure leaves stale materials (the connector-side relay
    token is already deleted, so the tunnel's next reconnect is rejected
    anyway), which is logged but not fatal. ``--no-start``: clearing materials
    from a stopped container must not cold-boot anything.
    """
    result = mngr_caller.call(
        [
            "exec",
            str(agent_id),
            f"rm -f {_SHARE_ENV_FILE} {_SHARE_GRANTS_FILE} {_SHARE_OWNER_EMAIL_FILE}",
            "--no-start",
        ],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("Failed to clear share materials from agent {}: {}", agent_id, result.stderr.strip())


def _extract_exec_stdout(exec_json_stdout: str) -> str | None:
    """Unwrap the remote command's own stdout from ``mngr exec --format json`` output.

    Returns None (with a warning logged) when the envelope is unparseable or
    reports the remote command as failed -- callers treat that as "the read
    never landed", never as file content.
    """
    try:
        envelope = json.loads(exec_json_stdout)
    except json.JSONDecodeError as exc:
        logger.warning("Unparseable mngr exec JSON envelope ({}): {}", exc, exec_json_stdout[:200])
        return None
    results = envelope.get("results") if isinstance(envelope, dict) else None
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict) or not isinstance(first.get("stdout"), str) or first.get("success") is not True:
        logger.warning("Unexpected mngr exec JSON envelope shape: {}", exec_json_stdout[:200])
        return None
    return first["stdout"]


def read_share_grants_from_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> str | None:
    """Read the grants document back from the agent; None when absent.

    The exec rides ``--format json`` and the document is unwrapped from the
    result envelope: in its default (human) format ``mngr exec`` appends its
    own ``Command succeeded on agent <name>`` status line to stdout, which is
    indistinguishable from file content and turned every raw read of a
    perfectly valid document into a "malformed grants" failure.

    A failed exec, or an unreadable envelope, raises
    :class:`ShareInjectionError` rather than returning None: "no document
    exists" and "the read never landed" must stay distinguishable, or a caller
    could mistake an unreadable policy for an empty one (the ``|| true`` folds
    the absent-file case into rc 0 with empty stdout).
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"cat {_SHARE_GRANTS_FILE} 2>/dev/null || true", "--no-start", "--format", "json"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ShareInjectionError(
            f"Could not read share grants from agent {agent_id}: {result.stderr.strip() or 'exec failed'}"
        )
    grants_text = _extract_exec_stdout(result.stdout)
    if grants_text is None:
        raise ShareInjectionError(f"Could not read share grants from agent {agent_id}: unrecognized exec output")
    return grants_text if grants_text.strip() else None
