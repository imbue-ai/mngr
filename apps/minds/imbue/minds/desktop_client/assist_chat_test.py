import shlex

from imbue.minds.desktop_client.assist_chat import ASSIST_CHAT_LABEL
from imbue.minds.desktop_client.assist_chat import build_assist_chat_mngr_args
from imbue.minds.desktop_client.assist_chat import generate_assist_chat_name
from imbue.minds.desktop_client.assist_chat import spawn_assist_chat
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


def test_build_assist_chat_targets_workspace_by_id_and_runs_create_inside() -> None:
    agent_id = AgentId.generate()
    args = build_assist_chat_mngr_args(
        workspace_agent_id=agent_id,
        workspace_name="my-workspace",
        description="the database migration failed",
        chat_name="assist-abc123",
    )
    # Outer: exec targets the workspace agent by id (a bare id is a valid agent address),
    # and carries a single inner-command string.
    assert args[:3] == ["exec", "--agent", str(agent_id)]
    assert len(args) == 4
    inner = shlex.split(args[3])
    # Inner: a chat-template create on the existing host, tagged so the system interface
    # auto-opens its tab, grouped with its workspace, seeded with /assist <description>.
    assert inner[0:3] == ["mngr", "create", "assist-abc123"]
    assert "--template" in inner and inner[inner.index("--template") + 1] == "chat"
    assert "--transfer" in inner and inner[inner.index("--transfer") + 1] == "none"
    assert "--no-connect" in inner
    assert f"{ASSIST_CHAT_LABEL}=true" in inner
    assert "workspace=my-workspace" in inner
    assert inner[-2:] == ["--message", "/assist the database migration failed"]


def test_build_assist_chat_omits_workspace_label_when_name_unknown() -> None:
    args = build_assist_chat_mngr_args(
        workspace_agent_id=AgentId.generate(),
        workspace_name=None,
        description="broken",
        chat_name="assist-x",
    )
    inner = shlex.split(args[3])
    assert not any(token.startswith("workspace=") for token in inner)


def test_build_assist_chat_quotes_description_so_it_cannot_break_the_shell_command() -> None:
    # ``mngr exec`` runs the inner command through a shell, so a description with shell
    # metacharacters must stay contained in the single --message argument.
    hostile = 'oops"; rm -rf /; echo $(whoami) `id` && touch /tmp/pwned'
    args = build_assist_chat_mngr_args(
        workspace_agent_id=AgentId.generate(),
        workspace_name="ws",
        description=hostile,
        chat_name="assist-x",
    )
    inner = shlex.split(args[3])
    # The whole /assist message survives as exactly one token -- nothing leaks out as
    # separate shell words or commands.
    assert inner[-2] == "--message"
    assert inner[-1] == f"/assist {hostile}"


def test_generate_assist_chat_name_is_prefixed_and_unique() -> None:
    first = generate_assist_chat_name()
    second = generate_assist_chat_name()
    assert first.startswith("assist-")
    assert first != second


def test_spawn_assist_chat_succeeds_and_passes_the_built_args() -> None:
    # A zero exit maps to True, and the caller is handed exactly the argv that
    # build_assist_chat_mngr_args assembles for the same inputs.
    caller = RecordingMngrCaller()
    agent_id = AgentId.generate()
    succeeded = spawn_assist_chat(
        mngr_caller=caller,
        workspace_agent_id=agent_id,
        workspace_name="my-workspace",
        description="it broke",
        chat_name="assist-abc123",
    )
    assert succeeded is True
    assert caller.calls == [
        build_assist_chat_mngr_args(
            workspace_agent_id=agent_id,
            workspace_name="my-workspace",
            description="it broke",
            chat_name="assist-abc123",
        )
    ]


def test_spawn_assist_chat_returns_false_on_nonzero_exit() -> None:
    # A non-zero ``mngr create`` exit surfaces as False so the /help/assist route returns 502.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="boom"))
    succeeded = spawn_assist_chat(
        mngr_caller=caller,
        workspace_agent_id=AgentId.generate(),
        workspace_name="ws",
        description="it broke",
        chat_name="assist-x",
    )
    assert succeeded is False
