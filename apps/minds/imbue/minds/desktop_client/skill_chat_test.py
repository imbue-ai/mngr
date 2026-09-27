import shlex
import subprocess
from pathlib import Path

import pytest

from imbue.minds.desktop_client.chat_app import MESSAGE_CHAT_SCRIPT
from imbue.minds.desktop_client.chat_app import read_chat_app_answer
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_BEGIN_SENTINEL
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_END_SENTINEL
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_EXIT_SENTINEL
from imbue.minds.desktop_client.skill_chat import AUTO_OPEN_CHAT_LABELS
from imbue.minds.desktop_client.skill_chat import AccountBindingState
from imbue.minds.desktop_client.skill_chat import NEEDS_ACCOUNT_BINDING_SENTINEL
from imbue.minds.desktop_client.skill_chat import SKIP_CLAUDE_INSTALLATION_CHECK_SETTING
from imbue.minds.desktop_client.skill_chat import SkillChatLaunchOutcome
from imbue.minds.desktop_client.skill_chat import USER_CREATED_LABEL
from imbue.minds.desktop_client.skill_chat import build_account_binding_probe_args
from imbue.minds.desktop_client.skill_chat import build_create_chat_script_args
from imbue.minds.desktop_client.skill_chat import build_skill_chat_launch_command
from imbue.minds.desktop_client.skill_chat import build_skill_chat_mngr_args
from imbue.minds.desktop_client.skill_chat import generate_chat_name
from imbue.minds.desktop_client.skill_chat import launch_skill_chat
from imbue.minds.desktop_client.skill_chat import resolve_account_binding
from imbue.minds.desktop_client.skill_chat import skill_sentinel
from imbue.minds.desktop_client.testing import account_binding_probe_stdout
from imbue.minds.desktop_client.testing import exec_error_stdout
from imbue.minds.desktop_client.testing import exec_result_stdout
from imbue.minds.desktop_client.testing import host_offline_exec_result
from imbue.minds.desktop_client.testing import skill_chat_launch_stdout
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.api.exec import COMMAND_EXECUTION_FAILURE_PREFIX
from imbue.mngr.cli.exec import exec_command
from imbue.mngr.primitives import AgentId

# Stands in for the template's messaging script: records its argv and exits as told.
_STUB_MESSAGE_CHAT_SCRIPT = """
import pathlib, sys
pathlib.Path("script-argv").write_text("\\0".join(sys.argv[1:]))
sys.exit(int(pathlib.Path("script-exit-code").read_text()))
"""


def _make_workspace(tmp_path: Path, *, skill_name: str, script_exit_code: int) -> Path:
    """A work dir with the skill, a stub messaging script, and a resolver script but no local settings.

    That is a legacy (minds-v0.5.0 through v0.5.2) workspace, whose fallback says it needs a
    binding rather than running a real ``mngr create``.
    """
    skill_dir = tmp_path / ".agents" / "skills" / skill_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# skill\n")
    scripts_dir = tmp_path / "system" / "scripts"
    scripts_dir.mkdir(parents=True)
    (tmp_path / MESSAGE_CHAT_SCRIPT).write_text(_STUB_MESSAGE_CHAT_SCRIPT)
    (scripts_dir / "default_account_args.py").write_text("")
    (tmp_path / "script-exit-code").write_text(str(script_exit_code))
    return tmp_path


def _run_launch_command(work_dir: Path, *, skill_name: str, message: str) -> MngrCallResult:
    """Run the real launch shell in ``work_dir`` with a plain ``python3``, answering as ``mngr exec --format jsonl`` would."""
    command = build_skill_chat_launch_command(skill_name=skill_name, chat_name="chat-x", message=message)
    completed = subprocess.run(["sh", "-c", command], cwd=work_dir, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return MngrCallResult(returncode=0, stdout=exec_result_stdout(completed.stdout, completed.stderr))


def test_the_launch_shell_refuses_a_workspace_without_the_skill_before_touching_the_chat_app(tmp_path: Path) -> None:
    (tmp_path / "script-exit-code").write_text("0")

    result = _run_launch_command(tmp_path, skill_name="assist", message="/assist it broke")

    assert skill_sentinel("assist", "ABSENT") in result.stdout
    assert skill_sentinel("assist", "PRESENT") not in result.stdout
    assert not (tmp_path / "script-argv").exists()


def test_the_launch_shell_asks_the_chat_app_with_the_seed_message_intact(tmp_path: Path) -> None:
    """The message reaches the script as one argument, whatever shell metacharacters it carries."""
    work_dir = _make_workspace(tmp_path, skill_name="assist", script_exit_code=0)
    hostile = 'oops"; rm -rf /; echo $(whoami) `id` && touch /tmp/pwned\n\nsecond line'

    result = _run_launch_command(work_dir, skill_name="assist", message=hostile)
    answer = read_chat_app_answer(result)

    assert skill_sentinel("assist", "PRESENT") in result.stdout
    assert answer.is_delivered
    assert answer.fallback is None
    script_argv = (work_dir / "script-argv").read_text().split("\0")
    assert script_argv == build_create_chat_script_args(chat_name="chat-x", message=hostile)


def test_the_launch_shell_falls_back_in_the_same_run_when_the_script_has_no_create_mode(tmp_path: Path) -> None:
    """argparse's 2 is no verdict, so the fallback runs right behind it; on a legacy workspace it asks for a binding."""
    work_dir = _make_workspace(tmp_path, skill_name="update-self", script_exit_code=2)

    answer = read_chat_app_answer(_run_launch_command(work_dir, skill_name="update-self", message="/update-self"))

    assert answer.fallback is not None
    assert answer.fallback.stdout.strip() == NEEDS_ACCOUNT_BINDING_SENTINEL
    assert answer.fallback.exit_code == 0


def test_the_launch_is_one_exec_that_never_boots_a_stopped_workspace() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=skill_chat_launch_stdout("assist")))
    agent_id = AgentId.generate()

    launch = launch_skill_chat(caller, str(agent_id), skill_name="assist", chat_name="assist-1", message="/assist x")

    assert launch.outcome is SkillChatLaunchOutcome.STARTED
    assert len(caller.calls) == 1
    context = exec_command.make_context("mngr exec", caller.calls[0][1:])
    assert [address.agent for address in context.params["agent_list"]] == [str(agent_id)]
    assert context.params["start"] is False


@pytest.mark.parametrize(
    ("stdout", "outcome", "detail"),
    (
        (skill_chat_launch_stdout("assist", is_skill_present=False), SkillChatLaunchOutcome.UNSUPPORTED, ""),
        (skill_chat_launch_stdout("assist", script_exit_code=0), SkillChatLaunchOutcome.STARTED, ""),
        (
            skill_chat_launch_stdout(
                "assist",
                script_exit_code=1,
                script_stderr="Retrying\nThe chat app did not create the chat: mngr create exited with code 1\n",
            ),
            SkillChatLaunchOutcome.SPAWN_FAILED,
            "The chat app did not create the chat: mngr create exited with code 1",
        ),
        (
            skill_chat_launch_stdout("assist", script_exit_code=2, fallback_stdout="", fallback_exit_code=0),
            SkillChatLaunchOutcome.STARTED,
            "",
        ),
        (
            skill_chat_launch_stdout(
                "assist",
                script_exit_code=2,
                fallback_stdout="",
                fallback_exit_code=1,
                fallback_stderr=(
                    "WARNING: something unrelated\nError: Unknown fields in agent_types.opencode: ['x']\n"
                ),
            ),
            SkillChatLaunchOutcome.SPAWN_FAILED,
            "Error: Unknown fields in agent_types.opencode: ['x']",
        ),
        (
            skill_chat_launch_stdout("assist", script_exit_code=2, fallback_stdout="", fallback_exit_code=None),
            SkillChatLaunchOutcome.SPAWN_FAILED,
            "",
        ),
    ),
    ids=(
        "no-skill",
        "chat-app-created",
        "chat-app-refused",
        "bare-create-landed",
        "bare-create-refused",
        "bare-create-cut-off",
    ),
)
def test_one_launch_exec_answers_what_the_workspace_made_of_it(
    stdout: str, outcome: SkillChatLaunchOutcome, detail: str
) -> None:
    """A refusal is final and in the workspace's own words; nothing is tried again behind it."""
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=stdout))

    launch = launch_skill_chat(caller, "agent-1", skill_name="assist", chat_name="assist-1", message="/assist x")

    assert launch.outcome is outcome
    assert launch.failure_detail == detail
    assert len(caller.calls) == 1


def test_a_launch_that_never_reached_the_workspace_is_unreachable() -> None:
    """Nothing ran there, which is what lets a caller start the workspace and launch again."""
    caller = RecordingMngrCaller(result=host_offline_exec_result())

    launch = launch_skill_chat(caller, "agent-1", skill_name="assist", chat_name="assist-1", message="/assist x")

    assert launch.outcome is SkillChatLaunchOutcome.UNREACHABLE


def test_a_launch_cut_off_after_reaching_the_workspace_is_a_failure_not_unreachable() -> None:
    """A connection lost mid-launch leaves the create done or in flight, so it must not cue a start and a second launch."""
    reason = f"{COMMAND_EXECUTION_FAILURE_PREFIX} agent-1: Connection reset by peer"
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=1, stdout=exec_error_stdout(reason), is_mngr_output=True)
    )

    launch = launch_skill_chat(caller, "agent-1", skill_name="assist", chat_name="assist-1", message="/assist x")

    assert launch.outcome is SkillChatLaunchOutcome.SPAWN_FAILED
    assert launch.failure_detail == reason


@pytest.mark.parametrize(
    "result",
    (
        MngrCallResult(
            returncode=-1,
            is_timed_out=True,
            stderr="mngr exec --agent a1 ... --message '/assist my laptop' timed out after 480s",
        ),
        MngrCallResult(returncode=1, stderr="mngr warm process exited without returning a result"),
    ),
    ids=("timed_out", "warm_process_died"),
)
def test_a_launch_mngr_never_answered_is_not_mistaken_for_one_that_never_ran(result: MngrCallResult) -> None:
    """It may have made the chat before it was cut off, so a caller that retried would make a second one.

    Its stderr is minds' own line (a timeout's quotes the seed message), so none of it is shown as the machine's words."""
    caller = RecordingMngrCaller(result=result)

    launch = launch_skill_chat(caller, "agent-1", skill_name="assist", chat_name="assist-1", message="/assist x")

    assert launch.outcome is SkillChatLaunchOutcome.SPAWN_FAILED
    assert launch.failure_detail == ""


def test_a_legacy_workspace_is_bound_through_its_resolver_before_its_bare_create() -> None:
    """Only a workspace that keeps accounts but writes no create defaults costs the two extra execs."""
    caller = ScriptedMngrCaller(
        results=(
            MngrCallResult(
                returncode=0,
                stdout=skill_chat_launch_stdout(
                    "update-self", script_exit_code=2, fallback_stdout=f"{NEEDS_ACCOUNT_BINDING_SENTINEL}\n"
                ),
            ),
            MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="/home/user/acc/a1")),
            MngrCallResult(returncode=0),
        )
    )
    agent_id = AgentId.generate()

    launch = launch_skill_chat(caller, str(agent_id), skill_name="update-self", chat_name="x", message="/update-self")

    assert launch.outcome is SkillChatLaunchOutcome.STARTED
    assert caller.calls[1] == build_account_binding_probe_args(str(agent_id))
    assert caller.calls[2] == build_skill_chat_mngr_args(
        str(agent_id),
        chat_name="x",
        message="/update-self",
        account_args=("--env", "CLAUDE_CONFIG_DIR=/home/user/acc/a1"),
    )


def test_the_chat_app_is_asked_for_a_labeled_chat_with_the_seed_message() -> None:
    script_args = build_create_chat_script_args(chat_name="assist-abc123", message="/assist it broke")
    assert script_args[:3] == ["--create", "--name", "assist-abc123"]
    labels = [script_args[i + 1] for i, token in enumerate(script_args) if token == "--label"]
    assert labels == [f"{label}=true" for label in AUTO_OPEN_CHAT_LABELS]
    # The route marks the chat user-created and binds the account itself.
    assert USER_CREATED_LABEL not in script_args and "--env" not in script_args
    assert script_args[-2:] == ["-m", "/assist it broke"]


def test_the_bare_create_runs_a_chat_create_inside_the_workspace_with_the_seed_message() -> None:
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
    assert USER_CREATED_LABEL in inner
    # No workspace grouping label: the chat lives in the container it was exec'd into.
    assert not any(token.startswith("workspace=") for token in inner)
    # No harness and no account: the workspace's own create defaults supply both.
    assert "--type" not in inner and "--env" not in inner
    assert inner[inner.index("-S") + 1] == SKIP_CLAUDE_INSTALLATION_CHECK_SETTING
    assert inner[-2:] == ["--message", "/assist it broke"]


def test_the_seed_message_cannot_break_out_of_the_bare_create() -> None:
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


def test_a_failed_legacy_bare_create_carries_the_machines_own_refusal() -> None:
    """The caller renders this; without it the user is told only to try again."""
    stderr = (
        "WARNING: outer SSH unreachable for host host-other: Host not found: host-other\n"
        "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']\n"
        "ERROR: Command failed on agent system-services\n"
    )
    caller = ScriptedMngrCaller(
        results=(
            MngrCallResult(
                returncode=0,
                stdout=skill_chat_launch_stdout(
                    "assist", script_exit_code=2, fallback_stdout=f"{NEEDS_ACCOUNT_BINDING_SENTINEL}\n"
                ),
            ),
            MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="")),
            MngrCallResult(returncode=1, stderr=stderr, is_mngr_output=True),
        )
    )

    launch = launch_skill_chat(caller, "agent-1", skill_name="assist", chat_name="x", message="/assist it broke")

    assert launch.outcome is SkillChatLaunchOutcome.SPAWN_FAILED
    assert launch.failure_detail.startswith("Error: Unknown fields in agent_types.opencode")
    # The unrelated host is the first thing mngr prints and the last thing to blame.
    assert "outer SSH unreachable" not in launch.failure_detail


# CLEANUP: the resolver tests below go with the resolver (see skill_chat.py).


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
