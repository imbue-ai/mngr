import shlex

from imbue.minds.desktop_client.skill_chat import AUTO_OPEN_CHAT_LABELS
from imbue.minds.desktop_client.skill_chat import SkillSupport
from imbue.minds.desktop_client.skill_chat import build_skill_chat_mngr_args
from imbue.minds.desktop_client.skill_chat import build_skill_support_probe_args
from imbue.minds.desktop_client.skill_chat import check_skill_support
from imbue.minds.desktop_client.skill_chat import generate_chat_name
from imbue.minds.desktop_client.skill_chat import spawn_skill_chat
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


def test_the_probe_targets_the_workspace_and_checks_the_named_skill_file() -> None:
    agent_id = AgentId.generate()
    args = build_skill_support_probe_args(agent_id, "update-self")
    assert args[:3] == ["exec", "--agent", str(agent_id)]
    # Probes run eagerly (a modal opening, a dispatch); they must never boot a
    # stopped workspace as a side effect (mngr exec auto-starts by default).
    assert "--no-start" in args
    assert len(args) == 5
    assert ".agents/skills/update-self/SKILL.md" in args[3]


def test_a_present_skill_reads_as_supported_and_makes_exactly_one_probe_call() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_UPDATE_SELF_SKILL_PRESENT\n"))
    agent_id = AgentId.generate()
    assert check_skill_support(caller, agent_id, "update-self") is SkillSupport.SUPPORTED
    assert caller.calls == [build_skill_support_probe_args(agent_id, "update-self")]


def test_an_absent_skill_reads_as_unsupported_rather_than_unreachable() -> None:
    # A reachable workspace whose (older) template lacks the skill: absent sentinel on a clean exit.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_ABSENT\n"))
    assert check_skill_support(caller, AgentId.generate(), "assist") is SkillSupport.UNSUPPORTED


def test_a_probe_that_never_ran_reads_as_unreachable() -> None:
    # No sentinel in stdout (the exec failed / host down) must not be mistaken for "absent".
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="connection refused"))
    assert check_skill_support(caller, AgentId.generate(), "assist") is SkillSupport.UNREACHABLE


def test_one_skills_sentinel_does_not_vouch_for_another() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_PRESENT\n"))
    assert check_skill_support(caller, AgentId.generate(), "update-self") is SkillSupport.UNREACHABLE


def test_the_spawn_runs_a_chat_create_inside_the_workspace_with_the_seed_message() -> None:
    agent_id = AgentId.generate()
    args = build_skill_chat_mngr_args(agent_id, chat_name="assist-abc123", message="/assist it broke")
    # Outer: exec targets the workspace agent by id and carries one inner-command string.
    assert args[:3] == ["exec", "--agent", str(agent_id)]
    # The chat create must not boot a stopped workspace as a side effect
    # (mngr exec auto-starts the host by default).
    assert "--no-start" in args
    assert len(args) == 5
    inner = shlex.split(args[3])
    assert inner[0:3] == ["mngr", "create", "assist-abc123"]
    assert inner[inner.index("--template") + 1] == "chat"
    assert inner[inner.index("--transfer") + 1] == "none"
    assert "--no-connect" in inner
    for label in AUTO_OPEN_CHAT_LABELS:
        assert f"{label}=true" in inner
    # No workspace grouping label: the chat lives in the container it was exec'd into.
    assert not any(token.startswith("workspace=") for token in inner)
    assert inner[-2:] == ["--message", "/assist it broke"]


def test_the_seed_message_cannot_break_out_of_the_shell_command() -> None:
    # ``mngr exec`` runs the inner command through a shell, so metacharacters and
    # newlines in the message must stay inside the single --message argument.
    hostile = 'oops"; rm -rf /; echo $(whoami) `id` && touch /tmp/pwned\n\nsecond line'
    args = build_skill_chat_mngr_args(AgentId.generate(), chat_name="x", message=hostile)
    inner = shlex.split(args[3])
    assert inner[-2:] == ["--message", hostile]


def test_generated_chat_names_carry_the_skill_and_do_not_repeat() -> None:
    first = generate_chat_name("update-self")
    assert first.startswith("update-self-")
    assert first != generate_chat_name("update-self")


def test_a_successful_spawn_makes_exactly_the_built_call() -> None:
    caller = RecordingMngrCaller()
    agent_id = AgentId.generate()
    assert spawn_skill_chat(caller, agent_id, chat_name="assist-abc123", message="/assist it broke") is True
    assert caller.calls == [
        build_skill_chat_mngr_args(agent_id, chat_name="assist-abc123", message="/assist it broke")
    ]


def test_a_failed_spawn_is_reported_rather_than_swallowed() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="boom"))
    assert spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist it broke") is False
