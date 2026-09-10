import shlex

import pytest

from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_BEGIN_SENTINEL
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_END_SENTINEL
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_EXIT_SENTINEL
from imbue.minds.desktop_client.skill_chat import AUTO_OPEN_CHAT_LABELS
from imbue.minds.desktop_client.skill_chat import AccountBindingState
from imbue.minds.desktop_client.skill_chat import SkillSupport
from imbue.minds.desktop_client.skill_chat import build_account_binding_probe_args
from imbue.minds.desktop_client.skill_chat import build_skill_chat_mngr_args
from imbue.minds.desktop_client.skill_chat import build_skill_support_probe_args
from imbue.minds.desktop_client.skill_chat import check_skill_support
from imbue.minds.desktop_client.skill_chat import generate_chat_name
from imbue.minds.desktop_client.skill_chat import resolve_account_binding
from imbue.minds.desktop_client.skill_chat import spawn_skill_chat
from imbue.minds.desktop_client.testing import account_binding_probe_stdout
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
    args = build_skill_chat_mngr_args(agent_id, chat_name="assist-abc123", message="/assist it broke", account_args=())
    # Outer: exec targets the workspace agent by id and carries one inner-command string.
    assert args[:3] == ["exec", "--agent", str(agent_id)]
    # The chat create must not boot a stopped workspace as a side effect
    # (mngr exec auto-starts the host by default).
    assert "--no-start" in args
    assert len(args) == 5
    inner = shlex.split(args[3])
    # The env assignments lead: the image's mngr must win over a stale shell
    # PATH, and the workspace's own settings.toml must not be able to fail the
    # create at config parse (see in_workspace_mngr).
    assert inner[:2] == ["PATH=/root/.local/bin:$PATH", "MNGR_ALLOW_UNKNOWN_CONFIG=1"]
    assert inner[2:5] == ["mngr", "create", "assist-abc123"]
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
    args = build_skill_chat_mngr_args(AgentId.generate(), chat_name="x", message=hostile, account_args=())
    inner = shlex.split(args[3])
    assert inner[-2:] == ["--message", hostile]


def test_generated_chat_names_carry_the_skill_and_do_not_repeat() -> None:
    first = generate_chat_name("update-self")
    assert first.startswith("update-self-")
    assert first != generate_chat_name("update-self")


def test_a_successful_spawn_makes_exactly_the_built_call() -> None:
    caller = RecordingMngrCaller()
    agent_id = AgentId.generate()
    spawn = spawn_skill_chat(caller, agent_id, chat_name="assist-abc123", message="/assist it broke", account_args=())
    assert spawn.is_started is True
    assert spawn.failure_detail == ""
    assert caller.calls == [
        build_skill_chat_mngr_args(agent_id, chat_name="assist-abc123", message="/assist it broke", account_args=())
    ]


def test_a_failed_spawn_carries_the_machines_own_refusal() -> None:
    """The caller renders this; without it the user is told only to try again."""
    stderr = (
        "WARNING: outer SSH unreachable for host host-other: Host not found: host-other\n"
        "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']\n"
        "ERROR: Command failed on agent system-services\n"
    )
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr=stderr, is_mngr_output=True))

    spawn = spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist it broke", account_args=())

    assert spawn.is_started is False
    assert spawn.failure_detail.startswith("Error: Unknown fields in agent_types.opencode")
    # The unrelated host is the first thing mngr prints and the last thing to blame.
    assert "outer SSH unreachable" not in spawn.failure_detail


@pytest.mark.parametrize(
    "result",
    (
        # The timeout line quotes the whole argv, the seed message with it.
        MngrCallResult(
            returncode=-1,
            is_timed_out=True,
            stderr="mngr exec --agent a1 MNGR_ALLOW_UNKNOWN_CONFIG=1 mngr create x --message '/assist my laptop'"
            " timed out after 120s",
        ),
        # A warm process that died before answering. It carries no verdict
        # marker, so the whole line would be quoted as the machine's words.
        MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result"),
    ),
    ids=("timed_out", "warm_process_died"),
)
def test_a_spawn_the_workspace_never_answered_quotes_nothing_at_the_user(result: MngrCallResult) -> None:
    """These stderrs are minds' own lines, so showing them as the machine's verdict misattributes our own fault."""
    caller = RecordingMngrCaller(result=result)

    spawn = spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist my laptop", account_args=())

    assert spawn.is_started is False
    assert spawn.failure_detail == ""


def test_the_account_probe_asks_the_template_for_the_binding_without_booting_the_machine() -> None:
    agent_id = AgentId.generate()
    args = build_account_binding_probe_args(agent_id)
    assert args[:3] == ["exec", "--agent", str(agent_id)]
    assert "--no-start" in args
    assert len(args) == 5
    # The script path is the contract; the package behind it is not. The chat create-template
    # makes a claude agent, and a resolver asked for the wrong harness launches unbound.
    assert "system/scripts/default_account_args.py claude" in args[3]
    # A resolver that declines says why on stderr and nothing on stdout, so discarding stderr
    # would leave a refused update with no diagnosis anywhere.
    assert "2>/dev/null" not in args[3]


def test_a_resolved_account_carries_the_arguments_that_bind_the_chat() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="/home/user/acc/a1"))
    )

    binding = resolve_account_binding(caller, AgentId.generate())

    assert binding.state is AccountBindingState.BOUND
    assert binding.create_args == ("--env", "CLAUDE_CONFIG_DIR=/home/user/acc/a1")


def test_a_template_with_no_account_store_binds_nothing_rather_than_refusing() -> None:
    """Before the account store every agent shared one config dir, so an unbound chat is the authenticated one."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir=None))
    )

    binding = resolve_account_binding(caller, AgentId.generate())

    assert binding.state is AccountBindingState.NOT_REQUIRED
    assert binding.create_args == ()


def test_a_machine_that_resolves_no_account_is_unavailable_rather_than_unbound() -> None:
    """An empty answer is still an answer: spawning here would hand the user a chat that cannot take a turn."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir=""))
    )

    assert resolve_account_binding(caller, AgentId.generate()).state is AccountBindingState.UNAVAILABLE


def test_an_account_probe_that_never_ran_reads_as_unreachable() -> None:
    """No fence in stdout must not be mistaken for "this machine keeps no accounts"."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="connection refused"))

    assert resolve_account_binding(caller, AgentId.generate()).state is AccountBindingState.UNREACHABLE


@pytest.mark.parametrize(
    "body",
    (
        "--env\n",
        "--env\nCLAUDE_CONFIG_DIR=/a\n--env\n",
        "--extra-provision-command\nln -sfn /a /b\n",
        "--env\n=novalue\n",
    ),
    ids=("flag-with-no-value", "odd-count", "not-the-env-form", "empty-name"),
)
def test_an_answer_that_cannot_be_read_is_not_spliced_into_a_create(body: str) -> None:
    """Splicing an unreadable answer is how a chat ends up bound to nothing, which is the bug this prevents.

    A resolver whose answer will not parse is a broken one, not a machine with nobody signed in,
    so it must not be reported as one signing in would fix.
    """
    stdout = f"{ACCOUNT_ARGS_BEGIN_SENTINEL}\n{body}{ACCOUNT_ARGS_END_SENTINEL}\n{ACCOUNT_ARGS_EXIT_SENTINEL}0\n"
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=stdout))

    binding = resolve_account_binding(caller, AgentId.generate())

    assert binding.state is AccountBindingState.UNREACHABLE
    assert binding.create_args == ()


def test_a_resolver_that_broke_is_not_reported_as_a_machine_with_no_account() -> None:
    """It exits non-zero only when it broke, and "sign in and try again" cannot fix a broken resolver."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="", exit_code=127))
    )

    assert resolve_account_binding(caller, AgentId.generate()).state is AccountBindingState.UNREACHABLE


def test_an_answer_that_never_reported_a_status_reads_as_unreachable() -> None:
    """A fence with no status behind it is a probe that was cut short, not a machine naming no account."""
    stdout = f"{ACCOUNT_ARGS_BEGIN_SENTINEL}\n{ACCOUNT_ARGS_END_SENTINEL}\n"
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=stdout))

    assert resolve_account_binding(caller, AgentId.generate()).state is AccountBindingState.UNREACHABLE


def test_the_spawned_create_binds_the_chat_to_the_resolved_account() -> None:
    """Without this the chat resolves a config dir holding no credential and answers every turn "Not logged in"."""
    account_args = ("--env", "CLAUDE_CONFIG_DIR=/home/user/.minds/accounts/a1")
    args = build_skill_chat_mngr_args(
        AgentId.generate(), chat_name="update-self-abc123", message="/update-self", account_args=account_args
    )
    inner = shlex.split(args[3])

    assert inner[inner.index("--env") + 1] == "CLAUDE_CONFIG_DIR=/home/user/.minds/accounts/a1"
    # The seed message stays the final argument, so the binding cannot swallow it.
    assert inner[-2:] == ["--message", "/update-self"]
