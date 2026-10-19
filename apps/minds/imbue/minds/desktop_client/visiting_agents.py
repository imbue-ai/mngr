"""Visiting agents: the desktop's side of inviting an outside coding agent into a workspace.

Everything is decided and stored inside the workspace (the share gateway's
``agent-access`` CLI, ``specs/studio-agent-access/spec.md``); the desktop only
runs that CLI over ``mngr exec`` with the signed-in account as the granter and
shows what it answers. Listing works on a stopped workspace's running machine
only (``--no-start``); minting an invite needs the workspace running and
published, which the route arranges before calling here.
"""

import json
import re
import shlex
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.mngr_command import extract_exec_failure_detail
from imbue.minds.desktop_client.mngr_command import extract_exec_stdout
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCaller

_AGENT_ACCESS_COMMAND: Final[str] = "uv run --no-sync agent-access"
_AGENT_CLI_FILE: Final[str] = "system/services/share_gateway/src/share_gateway/agent_cli.py"
_EXEC_TIMEOUT_SECONDS: Final[float] = 90.0
UNSUPPORTED_WORKSPACE_MESSAGE: Final[str] = (
    "This workspace predates visiting agents; update it (the update-self skill) before inviting one"
)
_REFUSAL_KIND_UNSUPPORTED: Final[str] = "unsupported"
_REFUSAL_KIND_REFUSED: Final[str] = "refused"
# The id shape the template mints (spec section 5.2): "va-" and 32 hex characters.
_VISITING_AGENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^va-[0-9a-f]{32}$")


class VisitingAgentsError(MindError):
    """Raised when the workspace's agent-access CLI could not be run or answered something unreadable."""


class VisitingAgentsRefusedError(VisitingAgentsError):
    """Raised when the CLI refused the operation (a bad scope, an unknown agent, no such grant)."""


class VisitingAgentsUnsupportedError(VisitingAgentsError):
    """Raised when the workspace's template has no agent-access CLI (it predates visiting agents)."""


class VisitingAgent(FrozenModel):
    """One visiting agent's grant as the workspace reports it (never its key material or token)."""

    agent_id: str = Field(description="The grant's id (va-<hex>)")
    name: str = Field(description="The name the agent gave itself")
    fingerprint: str = Field(description="The SHA256 fingerprint of the agent's key")
    scope: str | tuple[str, ...] = Field(description="'workspace', or the app names the grant admits")
    granter: str = Field(description="Who invited it: user:<id> or agent:<id>")
    created_at: str = Field(description="When the grant was made (ISO 8601)")
    expires_at: str | None = Field(default=None, description="When the grant expires, if ever")
    last_seen_at: str | None = Field(default=None, description="When the agent last made a request, if ever")


class VisitingAgentInvite(FrozenModel):
    """A freshly minted one-time code and the instruction block an inviter copies."""

    code: str = Field(description="The one-time code (valid fifteen minutes, consumed by one join)")
    expires_at: str = Field(description="When the code expires (ISO 8601)")
    scope: str | tuple[str, ...] = Field(description="'workspace', or the app names the grant will admit")
    granter: str = Field(description="The granter recorded on the invite")
    auth_origin: str = Field(description="The workspace's auth origin the agent joins at ('' while unpublished)")
    is_published: bool = Field(
        description="Whether the workspace holds its share materials, which the auth origin comes from"
    )
    instructions: str = Field(description="The instruction block to hand the agent")


@pure
def is_visiting_agent_id(value: str) -> bool:
    """Whether ``value`` has the shape of a visiting agent id, so a route can refuse junk before an exec."""
    return _VISITING_AGENT_ID_PATTERN.match(value) is not None


@pure
def scope_argument(scope: str | Sequence[str]) -> str:
    """The ``--scope`` value the CLI takes: ``workspace`` or a comma-separated list of app names."""
    if isinstance(scope, str):
        return scope
    return ",".join(scope)


@pure
def _scope_from_answer(raw: object) -> str | tuple[str, ...]:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and all(isinstance(name, str) for name in raw):
        return tuple(str(name) for name in raw)
    raise VisitingAgentsError(f"agent-access answered an unreadable scope: {raw!r}")


@pure
def _json_object(raw: object) -> dict[str, object] | None:
    """``raw`` as a JSON object with string keys, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    return {str(key): value for key, value in raw.items()}


@pure
def agent_access_command(arguments: Sequence[str]) -> str:
    """The shell ``mngr exec`` runs: the CLI with ``--json``, behind a check that the template ships it.

    A workspace from a template older than visiting agents has no ``agent-access``
    entry point; running it there fails with uv's own noise, so the command
    first looks for the CLI's module and otherwise answers the same JSON refusal
    the CLI would, with kind ``unsupported``.
    """
    cli = f"{_AGENT_ACCESS_COMMAND} {' '.join(shlex.quote(argument) for argument in arguments)} --json"
    refusal = json.dumps({"error": UNSUPPORTED_WORKSPACE_MESSAGE, "kind": _REFUSAL_KIND_UNSUPPORTED})
    return f"if [ -f {_AGENT_CLI_FILE} ]; then {cli}; else echo {shlex.quote(refusal)} >&2; exit 3; fi"


@pure
def parse_refusal(detail: str) -> tuple[str, str] | None:
    """The ``(kind, message)`` of a JSON refusal on the CLI's stderr, or None when the text is not one.

    The CLI prints the refusal as the last line of stderr; anything before it
    (uv's own output) is ignored.
    """
    lines = [line for line in detail.strip().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        parsed = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    kind, message = parsed.get("kind"), parsed.get("error")
    if not isinstance(kind, str) or not isinstance(message, str):
        return None
    return kind, message


def _run_agent_access(
    agent_address: str, mngr_caller: MngrCaller, arguments: Sequence[str], is_start_allowed: bool
) -> dict[str, object]:
    """Run one ``agent-access`` subcommand inside the workspace and parse its ``--json`` answer."""
    argv = ["exec", agent_address, agent_access_command(arguments), "--format", "json"]
    if not is_start_allowed:
        argv.append("--no-start")
    result = mngr_caller.call(argv, timeout=_EXEC_TIMEOUT_SECONDS)
    if result.returncode != 0:
        # The error carries a parsed, capped detail; the whole output is kept here.
        logger.warning(
            "agent-access failed in {} (rc={}); stdout={} stderr={}",
            agent_address,
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )
        # In json mode the command's stderr rides the envelope; mngr's own stderr covers an exec that never ran.
        detail = extract_exec_failure_detail(result.stdout) or result.stderr.strip()
        refusal = parse_refusal(detail)
        if refusal is not None:
            kind, message = refusal
            if kind == _REFUSAL_KIND_UNSUPPORTED:
                raise VisitingAgentsUnsupportedError(message)
            if kind == _REFUSAL_KIND_REFUSED:
                raise VisitingAgentsRefusedError(message)
            raise VisitingAgentsError(f"agent-access in {agent_address} is {kind}: {message}")
        raise VisitingAgentsError(f"Could not run agent-access in {agent_address}: {detail or 'exec failed'}")
    stdout = extract_exec_stdout(result.stdout)
    if stdout is None:
        raise VisitingAgentsError(f"Could not read agent-access's answer from {agent_address}")
    try:
        parsed = json.loads(stdout)
    except ValueError as exc:
        logger.warning("agent-access in {} answered something other than JSON: {}", agent_address, stdout)
        raise VisitingAgentsError(f"agent-access answered something other than JSON: {stdout[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise VisitingAgentsError("agent-access answered something other than a JSON object")
    return parsed


def list_visiting_agents(agent_address: str, mngr_caller: MngrCaller) -> list[VisitingAgent]:
    """The workspace's visiting agents, as its CLI lists them. Raises VisitingAgentsError."""
    answer = _run_agent_access(agent_address, mngr_caller, ["list"], is_start_allowed=False)
    raw_agents = answer.get("agents")
    if not isinstance(raw_agents, list):
        raise VisitingAgentsError("agent-access list answered no agents array")
    agents: list[VisitingAgent] = []
    for raw_entry in raw_agents:
        entry = _json_object(raw_entry)
        if entry is None:
            raise VisitingAgentsError("agent-access list answered a malformed entry")
        agents.append(
            VisitingAgent(
                agent_id=str(entry.get("agent_id", "")),
                name=str(entry.get("name", "")),
                fingerprint=str(entry.get("fingerprint", "")),
                scope=_scope_from_answer(entry.get("scope")),
                granter=str(entry.get("granter", "")),
                created_at=str(entry.get("created_at", "")),
                expires_at=_optional_text(entry.get("expires_at")),
                last_seen_at=_optional_text(entry.get("last_seen_at")),
            )
        )
    return agents


def mint_visiting_agent_invite(
    agent_address: str,
    mngr_caller: MngrCaller,
    granter_user_id: str,
    scope: str | Sequence[str],
    expires_in_days: float | None,
    name_hint: str,
) -> VisitingAgentInvite:
    """Mint a one-time code in the workspace with the signed-in account as the granter."""
    arguments = ["invite", "--granter", f"user:{granter_user_id}", "--scope", scope_argument(scope)]
    if expires_in_days is not None:
        arguments.extend(["--expires-in-days", str(expires_in_days)])
    if name_hint:
        arguments.extend(["--name-hint", name_hint])
    answer = _run_agent_access(agent_address, mngr_caller, arguments, is_start_allowed=True)
    return VisitingAgentInvite(
        code=str(answer.get("code", "")),
        expires_at=str(answer.get("expires_at", "")),
        scope=_scope_from_answer(answer.get("scope")),
        granter=str(answer.get("granter", "")),
        auth_origin=str(answer.get("auth_origin", "")),
        is_published=answer.get("is_published") is True,
        instructions=str(answer.get("instructions", "")),
    )


def revoke_visiting_agent(agent_address: str, mngr_caller: MngrCaller, agent_id: str, revoker_user_id: str) -> str:
    """Revoke one visiting agent as the workspace owner; answers the revoked id. Raises VisitingAgentsRefusedError."""
    answer = _run_agent_access(
        agent_address,
        mngr_caller,
        ["revoke", agent_id, "--revoker", f"user:{revoker_user_id}", "--owner"],
        is_start_allowed=True,
    )
    return str(answer.get("revoked", agent_id))


@pure
def _optional_text(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None
