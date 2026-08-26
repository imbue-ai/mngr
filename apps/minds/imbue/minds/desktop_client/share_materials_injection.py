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
import binascii
import threading
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.mngr_command import extract_exec_stdout
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


def _atomic_write_clause(relative_path: str, content: str, tmp_var: str) -> str:
    """One shell clause atomically writing ``content`` (base64 in transit) to ``relative_path``.

    The tmp name comes from mktemp, never a fixed `<path>.tmp`: two
    concurrent writers sharing one tmp path interleave their bytes and the
    loser's mv publishes a corrupted file. With a unique tmp per write the
    final mv stays atomic for readers and the last writer wins whole.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    directory, _, filename = relative_path.rpartition("/")
    return (
        f'mkdir -p {directory} && {tmp_var}="$(mktemp {directory}/.{filename}.XXXXXX)" '
        f"&& printf '%s' {encoded} | base64 -d > \"${tmp_var}\" "
        f'&& mv "${tmp_var}" {relative_path}'
    )


def provision_share_files_in_agent(
    agent_id: AgentId,
    grants_toml_text: str,
    owner_email: str,
    # None means "grants + owner email only" (the grants-only update path);
    # the running gateway re-reads grants per request, so share.env is
    # untouched and the tunnel never restarts.
    share_env_text: str | None,
    mngr_caller: MngrCaller,
) -> None:
    """Write all of a share's files into the agent in ONE exec round trip.

    Ordering inside the script matters: the grants document (and the owner
    email) land BEFORE share.env, because the share-gateway brings the whole
    stack up the moment share.env appears -- the grants must already be in
    place by then. The owner-email write is best-effort (services must
    tolerate its absence), so its clause is wrapped to never fail the exec;
    the grants and share.env writes are fatal.
    """
    clauses = [_atomic_write_clause(_SHARE_GRANTS_FILE, grants_toml_text, "tmp_grants")]
    if owner_email:
        owner_clause = _atomic_write_clause(_SHARE_OWNER_EMAIL_FILE, owner_email, "tmp_owner")
        clauses.append(f"{{ {owner_clause} || true; }}")
    else:
        logger.debug("Skipping owner-email injection for agent {}: no owner email", agent_id)
    if share_env_text is not None:
        clauses.append(_atomic_write_clause(_SHARE_ENV_FILE, share_env_text, "tmp_env"))
    result = mngr_caller.call(
        ["exec", str(agent_id), " && ".join(clauses)],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ShareInjectionError(f"Failed to write share files into agent {agent_id}: {result.stderr.strip()}")


_SHARE_GATEWAY_SERVICE_DIR: Final[str] = "system/services/share_gateway"

# Marker lines the state probe prints (parsed out of the exec JSON envelope so
# an SSH-level failure stays distinguishable from a negative answer).
_PROBE_GATEWAY_PREFIX: Final[str] = "MNGR_SHARE_GATEWAY="
_PROBE_SHARE_ENV_PREFIX: Final[str] = "MNGR_SHARE_ENV="
_PROBE_GRANTS_B64_PREFIX: Final[str] = "MNGR_SHARE_GRANTS_B64="
_PROBE_ABSENT_VALUE: Final[str] = "ABSENT"
_PROBE_UNREADABLE_VALUE: Final[str] = "UNREADABLE"

# One script answering everything the enable flow needs to know about the
# workspace, so the whole read costs a single exec round trip. The grants read
# is checked (a failed redirect/read fails the command substitution) so an
# existing-but-unreadable document reports UNREADABLE rather than looking
# absent -- the caller's data-loss guard depends on the distinction. `echo |
# tr` rather than `base64 -w0`: the workspace is a Debian container today, but
# the pipe form works on any base64 (echo is safe here -- base64 output never
# starts with a dash and carries no escapes).
_PROBE_SHARE_STATE_SCRIPT: Final[str] = (
    f"if test -d {_SHARE_GATEWAY_SERVICE_DIR}; then echo {_PROBE_GATEWAY_PREFIX}1; "
    f"else echo {_PROBE_GATEWAY_PREFIX}0; fi; "
    f"if test -f {_SHARE_ENV_FILE}; then echo {_PROBE_SHARE_ENV_PREFIX}1; "
    f"else echo {_PROBE_SHARE_ENV_PREFIX}0; fi; "
    f"if test -f {_SHARE_GRANTS_FILE}; then "
    f'if grants_b64="$(base64 < {_SHARE_GRANTS_FILE})"; then '
    f'echo "{_PROBE_GRANTS_B64_PREFIX}$(echo "$grants_b64" | tr -d \'\\n\')"; '
    f"else echo {_PROBE_GRANTS_B64_PREFIX}{_PROBE_UNREADABLE_VALUE}; fi; "
    f"else echo {_PROBE_GRANTS_B64_PREFIX}{_PROBE_ABSENT_VALUE}; fi"
)


class ShareAgentProbe(FrozenModel):
    """One-exec snapshot of the share-related state inside a workspace."""

    has_gateway: bool = Field(
        description=(
            "Whether the template ships the share-gateway service. Workspaces from a "
            "pre-share-gateway template (minds-v0.3.11 and older) have nothing watching "
            "share.env, so a share enabled for them can never come up. "
            "CLEANUP: this signal (and its caller's refusal) can be removed once no supported "
            "workspaces predate the share gateway -- i.e. after the first post-v0.3.11 release "
            "is deployed and the remaining old workspaces have run update-self."
        )
    )
    has_share_env: bool = Field(
        description=(
            "Whether share.env is present (the share stack's on-switch). Distinguishes an "
            "actively-shared workspace from one whose earlier enable failed between the "
            "connector-side create and the injection."
        )
    )
    grants_toml_text: str | None = Field(
        description="The current grants document's content, or None when no document exists."
    )


def probe_share_state_in_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> ShareAgentProbe:
    """Read the workspace's share state (gateway, share.env, grants) in one exec.

    Conservative on exec failure: everything reports absent, so the caller
    refuses with the actionable update-your-workspace message rather than
    provisioning a share that cannot work (a retry after the transient failure
    clears is cheap, and nothing has been written or created). A successful
    exec whose grants document exists but could not be read (the script's
    UNREADABLE marker), or whose grants payload cannot be decoded, raises
    :class:`ShareInjectionError` -- an unreadable existing policy must never
    be mistaken for an absent one.
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), _PROBE_SHARE_STATE_SCRIPT, "--no-start", "--format", "json"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.debug("Share state probe failed for agent {}: {}", agent_id, result.stderr.strip())
        return ShareAgentProbe(has_gateway=False, has_share_env=False, grants_toml_text=None)
    stdout = extract_exec_stdout(result.stdout)
    if stdout is None:
        return ShareAgentProbe(has_gateway=False, has_share_env=False, grants_toml_text=None)
    value_by_prefix: dict[str, str] = {}
    for line in stdout.splitlines():
        for prefix in (_PROBE_GATEWAY_PREFIX, _PROBE_SHARE_ENV_PREFIX, _PROBE_GRANTS_B64_PREFIX):
            if line.startswith(prefix):
                value_by_prefix[prefix] = line[len(prefix) :].strip()
    grants_value = value_by_prefix.get(_PROBE_GRANTS_B64_PREFIX, _PROBE_ABSENT_VALUE)
    if grants_value == _PROBE_UNREADABLE_VALUE:
        raise ShareInjectionError(f"The share grants document in agent {agent_id} exists but could not be read")
    if grants_value == _PROBE_ABSENT_VALUE or not grants_value:
        # An empty value is an empty (whitespace-free) document: the checked
        # read means a failed one reports UNREADABLE above, and an empty file
        # grants nobody -- same as absent (read_share_grants_from_agent agrees).
        grants_toml_text = None
    else:
        try:
            grants_toml_text = base64.b64decode(grants_value).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ShareInjectionError(f"Could not decode the share grants read from agent {agent_id}: {exc}") from exc
    return ShareAgentProbe(
        has_gateway=value_by_prefix.get(_PROBE_GATEWAY_PREFIX) == "1",
        has_share_env=value_by_prefix.get(_PROBE_SHARE_ENV_PREFIX) == "1",
        grants_toml_text=grants_toml_text,
    )


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
    grants_text = extract_exec_stdout(result.stdout)
    if grants_text is None:
        raise ShareInjectionError(f"Could not read share grants from agent {agent_id}: unrecognized exec output")
    return grants_text if grants_text.strip() else None
