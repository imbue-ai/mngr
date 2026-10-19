import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import Field

from imbue.minds.desktop_client.visiting_agents import VisitingAgentsError
from imbue.minds.desktop_client.visiting_agents import VisitingAgentsRefusedError
from imbue.minds.desktop_client.visiting_agents import VisitingAgentsUnsupportedError
from imbue.minds.desktop_client.visiting_agents import agent_access_command
from imbue.minds.desktop_client.visiting_agents import is_visiting_agent_id
from imbue.minds.desktop_client.visiting_agents import list_visiting_agents
from imbue.minds.desktop_client.visiting_agents import mint_visiting_agent_invite
from imbue.minds.desktop_client.visiting_agents import parse_refusal
from imbue.minds.desktop_client.visiting_agents import revoke_visiting_agent
from imbue.minds.desktop_client.visiting_agents import scope_argument
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller

_ADDRESS = "system-services@host-aaaa"


def exec_envelope(stdout: str, returncode: int = 0, stderr: str = "") -> MngrCallResult:
    envelope = {
        "results": [{"agent": "system-services", "stdout": stdout, "stderr": stderr, "success": returncode == 0}]
    }
    return MngrCallResult(returncode=returncode, stdout=json.dumps(envelope), stderr=stderr)


class AgentAccessCaller(RecordingMngrCaller):
    """Answers every exec with ``answer`` (a JSON document the CLI would print, or a canned failure)."""

    answer: MngrCallResult = Field(default_factory=lambda: exec_envelope("{}"))

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        super().call(argv, timeout=timeout, env_overrides=env_overrides, cwd=cwd)
        return self.answer


def test_is_visiting_agent_id_accepts_only_the_minted_shape() -> None:
    assert is_visiting_agent_id("va-" + "0123456789abcdef" * 2)
    assert not is_visiting_agent_id("va-" + "A" * 32)
    assert not is_visiting_agent_id("agent-" + "a" * 32)
    assert not is_visiting_agent_id("--json")
    assert not is_visiting_agent_id("")


def test_scope_argument_joins_app_names() -> None:
    assert scope_argument("workspace") == "workspace"
    assert scope_argument(["chat", "files"]) == "chat,files"


def test_agent_access_command_runs_the_cli_behind_a_template_capability_check() -> None:
    command = agent_access_command(["invite", "--name-hint", "Ada's Codex"])
    assert command.startswith("if [ -f system/services/share_gateway/src/share_gateway/agent_cli.py ]; then ")
    assert "then uv run --no-sync agent-access invite --name-hint 'Ada'\"'\"'s Codex' --json; else " in command
    refusal = command.split("; else echo ", 1)[1].split(" >&2; exit 3; fi")[0]
    assert json.loads(refusal.strip("'"))["kind"] == "unsupported"


def test_parse_refusal_reads_the_last_stderr_line_and_ignores_anything_else() -> None:
    assert parse_refusal('warning: uv noise\n{"error": "no agent va-x", "kind": "refused"}\n') == (
        "refused",
        "no agent va-x",
    )
    assert parse_refusal("ssh: connection refused") is None
    assert parse_refusal('{"error": 1}') is None
    assert parse_refusal("") is None


def test_list_visiting_agents_runs_the_cli_without_starting_the_workspace_and_parses_the_answer() -> None:
    caller = AgentAccessCaller(
        answer=exec_envelope(
            json.dumps(
                {
                    "agents": [
                        {
                            "agent_id": "va-" + "1" * 32,
                            "name": "Josh Claude",
                            "fingerprint": "SHA256:abc",
                            "scope": "workspace",
                            "granter": "user:owner",
                            "created_at": "2026-10-15T18:00:00+00:00",
                            "expires_at": None,
                            "last_seen_at": "2026-10-15T19:00:00+00:00",
                        },
                        {
                            "agent_id": "va-" + "2" * 32,
                            "name": "Ada Codex",
                            "fingerprint": "SHA256:def",
                            "scope": ["chat"],
                            "granter": "agent:va-" + "1" * 32,
                            "created_at": "2026-10-16T09:00:00+00:00",
                            "expires_at": "2026-10-23T09:00:00+00:00",
                            "last_seen_at": None,
                        },
                    ]
                }
            )
        )
    )

    agents = list_visiting_agents(_ADDRESS, caller)

    assert caller.calls == [["exec", _ADDRESS, agent_access_command(["list"]), "--format", "json", "--no-start"]]
    assert [agent.name for agent in agents] == ["Josh Claude", "Ada Codex"]
    assert agents[0].scope == "workspace" and agents[0].last_seen_at == "2026-10-15T19:00:00+00:00"
    assert agents[1].scope == ("chat",) and agents[1].expires_at == "2026-10-23T09:00:00+00:00"
    assert agents[1].last_seen_at is None


def test_mint_visiting_agent_invite_passes_the_granter_scope_expiry_and_hint_quoted() -> None:
    caller = AgentAccessCaller(
        answer=exec_envelope(
            json.dumps(
                {
                    "code": "7K3M-QX9A-2PFH",
                    "expires_at": "2026-10-15T18:15:00+00:00",
                    "scope": ["chat", "files"],
                    "granter": "user:8c0f",
                    "auth_origin": "https://auth-x.alpha.example.com",
                    "is_published": True,
                    "instructions": "Join with 7K3M-QX9A-2PFH",
                }
            )
        )
    )

    invite = mint_visiting_agent_invite(_ADDRESS, caller, "8c0f", ["chat", "files"], 7, "Ada's Codex")

    (call,) = caller.calls
    assert call[:2] == ["exec", _ADDRESS] and call[3:] == ["--format", "json"]
    assert call[2] == agent_access_command(
        [
            "invite",
            "--granter",
            "user:8c0f",
            "--scope",
            "chat,files",
            "--expires-in-days",
            "7",
            "--name-hint",
            "Ada's Codex",
        ]
    )
    assert invite.code == "7K3M-QX9A-2PFH" and invite.scope == ("chat", "files")
    assert invite.auth_origin == "https://auth-x.alpha.example.com" and "7K3M" in invite.instructions
    assert invite.is_published is True


def test_mint_visiting_agent_invite_reports_an_unpublished_workspace() -> None:
    caller = AgentAccessCaller(
        answer=exec_envelope(
            json.dumps(
                {
                    "code": "C",
                    "expires_at": "2026-10-15T18:15:00+00:00",
                    "scope": "workspace",
                    "granter": "user:8c0f",
                    "auth_origin": "",
                    "is_published": False,
                    "instructions": "Join at https://<the workspace's address once it is published>",
                }
            )
        )
    )
    invite = mint_visiting_agent_invite(_ADDRESS, caller, "8c0f", "workspace", None, "")
    assert invite.is_published is False and invite.auth_origin == ""


def test_revoke_visiting_agent_revokes_as_the_owner() -> None:
    caller = AgentAccessCaller(answer=exec_envelope(json.dumps({"revoked": "va-" + "1" * 32, "name": "x"})))
    assert revoke_visiting_agent(_ADDRESS, caller, "va-" + "1" * 32, "8c0f") == "va-" + "1" * 32
    (call,) = caller.calls
    assert call[2] == agent_access_command(["revoke", "va-" + "1" * 32, "--revoker", "user:8c0f", "--owner"])


def test_a_cli_refusal_is_reported_in_the_clis_words_and_other_failures_as_errors() -> None:
    # The CLI's stderr rides the exec envelope; mngr's own stderr says nothing in json mode.
    refused_answer = MngrCallResult(
        returncode=2,
        stdout=json.dumps(
            {
                "results": [
                    {
                        "agent": "system-services",
                        "stdout": "",
                        "stderr": '{"error": "no agent va-x", "kind": "refused"}\n',
                        "success": False,
                    }
                ]
            }
        ),
        stderr="",
    )
    with pytest.raises(VisitingAgentsRefusedError, match="no agent va-x"):
        revoke_visiting_agent(_ADDRESS, AgentAccessCaller(answer=refused_answer), "va-x", "8c0f")
    unsupported = AgentAccessCaller(
        answer=exec_envelope("", returncode=3, stderr='{"error": "update it first", "kind": "unsupported"}')
    )
    with pytest.raises(VisitingAgentsUnsupportedError, match="update it first"):
        list_visiting_agents(_ADDRESS, unsupported)
    misconfigured = AgentAccessCaller(
        answer=exec_envelope("", returncode=3, stderr='{"error": "grants.toml is bad", "kind": "misconfigured"}')
    )
    with pytest.raises(VisitingAgentsError, match="misconfigured: grants.toml is bad"):
        list_visiting_agents(_ADDRESS, misconfigured)
    broken = AgentAccessCaller(answer=MngrCallResult(returncode=1, stdout="", stderr="ssh: connection refused"))
    with pytest.raises(VisitingAgentsError, match="connection refused"):
        list_visiting_agents(_ADDRESS, broken)
    garbage = AgentAccessCaller(answer=exec_envelope("not json"))
    with pytest.raises(VisitingAgentsError, match="other than JSON"):
        list_visiting_agents(_ADDRESS, garbage)
