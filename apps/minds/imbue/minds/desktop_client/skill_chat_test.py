import shlex

import pytest

from imbue.minds.desktop_client.chat_app import MESSAGE_CHAT_SCRIPT
from imbue.minds.desktop_client.chat_app import build_message_chat_args
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_BEGIN_SENTINEL
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_END_SENTINEL
from imbue.minds.desktop_client.skill_chat import ACCOUNT_ARGS_EXIT_SENTINEL
from imbue.minds.desktop_client.skill_chat import AUTO_OPEN_CHAT_LABELS
from imbue.minds.desktop_client.skill_chat import AccountBindingState
from imbue.minds.desktop_client.skill_chat import LOCAL_SETTINGS_ABSENT_SENTINEL
from imbue.minds.desktop_client.skill_chat import LOCAL_SETTINGS_PRESENT_SENTINEL
from imbue.minds.desktop_client.skill_chat import SKIP_CLAUDE_INSTALLATION_CHECK_SETTING
from imbue.minds.desktop_client.skill_chat import SkillProbe
from imbue.minds.desktop_client.skill_chat import SkillSupport
from imbue.minds.desktop_client.skill_chat import USER_CREATED_LABEL
from imbue.minds.desktop_client.skill_chat import build_account_binding_probe_args
from imbue.minds.desktop_client.skill_chat import build_create_chat_script_args
from imbue.minds.desktop_client.skill_chat import build_skill_chat_mngr_args
from imbue.minds.desktop_client.skill_chat import build_skill_support_probe_args
from imbue.minds.desktop_client.skill_chat import generate_chat_name
from imbue.minds.desktop_client.skill_chat import probe_skill
from imbue.minds.desktop_client.skill_chat import resolve_account_binding
from imbue.minds.desktop_client.skill_chat import resolve_legacy_account_args
from imbue.minds.desktop_client.skill_chat import spawn_skill_chat
from imbue.minds.desktop_client.testing import account_binding_probe_stdout
from imbue.minds.desktop_client.testing import exec_error_stdout
from imbue.minds.desktop_client.testing import script_exit_stdout
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.utils.testing import ScriptedMngrCaller
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
    # The same exec answers whether the workspace writes its own create defaults.
    assert ".mngr/settings.local.toml" in args[3]


def test_a_present_skill_reads_as_supported_and_makes_exactly_one_probe_call() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_UPDATE_SELF_SKILL_PRESENT\n"))
    agent_id = AgentId.generate()
    assert probe_skill(caller, agent_id, "update-self").support is SkillSupport.SUPPORTED
    assert caller.calls == [build_skill_support_probe_args(agent_id, "update-self")]


def test_an_absent_skill_reads_as_unsupported_rather_than_unreachable() -> None:
    # A reachable workspace whose (older) template lacks the skill: absent sentinel on a clean exit.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_ABSENT\n"))
    assert probe_skill(caller, AgentId.generate(), "assist").support is SkillSupport.UNSUPPORTED


def test_a_probe_that_never_ran_reads_as_unreachable() -> None:
    # No sentinel in stdout (the exec failed / host down) must not be mistaken for "absent".
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="connection refused"))
    assert probe_skill(caller, AgentId.generate(), "assist").support is SkillSupport.UNREACHABLE


def test_one_skills_sentinel_does_not_vouch_for_another() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="MNGR_ASSIST_SKILL_PRESENT\n"))
    assert probe_skill(caller, AgentId.generate(), "update-self").support is SkillSupport.UNREACHABLE


@pytest.mark.parametrize(
    ("sentinel", "is_present"),
    ((LOCAL_SETTINGS_PRESENT_SENTINEL, True), (LOCAL_SETTINGS_ABSENT_SENTINEL, False), ("", False)),
    ids=("present", "absent", "unsaid"),
)
def test_the_probe_reports_whether_the_workspace_writes_its_create_defaults(sentinel: str, is_present: bool) -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=f"MNGR_ASSIST_SKILL_PRESENT\n{sentinel}\n")
    )

    probe = probe_skill(caller, AgentId.generate(), "assist")

    assert probe.support is SkillSupport.SUPPORTED
    assert probe.is_local_settings_present is is_present


_READY_PROBE = SkillProbe(support=SkillSupport.SUPPORTED, is_local_settings_present=True)
_SCRIPT_CREATED = MngrCallResult(returncode=0, stdout=script_exit_stdout(0, '{"chat_id": "agent-1"}\n'))
# A script from before its create mode (argparse's 2), or none at all (python's 2).
_SCRIPT_NO_VERDICT = MngrCallResult(
    returncode=0,
    stdout=script_exit_stdout(2, inner_stderr="message_chat.py: error: unrecognized arguments: --create\n"),
)


def _script_call(agent_id: AgentId, *, chat_name: str, message: str) -> list[str]:
    return build_message_chat_args(str(agent_id), build_create_chat_script_args(chat_name=chat_name, message=message))


def test_the_spawn_asks_the_chat_app_for_a_labeled_chat_with_the_seed_message() -> None:
    script_args = build_create_chat_script_args(chat_name="assist-abc123", message="/assist it broke")
    assert script_args[:3] == ["--create", "--name", "assist-abc123"]
    labels = [script_args[i + 1] for i, token in enumerate(script_args) if token == "--label"]
    assert labels == [f"{label}=true" for label in AUTO_OPEN_CHAT_LABELS]
    # The route marks the chat user-created and binds the account itself.
    assert USER_CREATED_LABEL not in script_args and "--env" not in script_args
    assert script_args[-2:] == ["-m", "/assist it broke"]


def test_the_seed_message_cannot_break_out_of_the_script_command() -> None:
    hostile = 'oops"; rm -rf /; echo $(whoami) `id` && touch /tmp/pwned\n\nsecond line'
    args = _script_call(AgentId.generate(), chat_name="x", message=hostile)
    script, _, _ = args[3].rpartition("; ")
    inner = shlex.split(script)
    assert inner[2:4] == ["python3", MESSAGE_CHAT_SCRIPT]
    assert inner[-2:] == ["-m", hostile]


def test_a_spawn_the_chat_app_made_makes_exactly_the_script_call() -> None:
    caller = RecordingMngrCaller(result=_SCRIPT_CREATED)
    agent_id = AgentId.generate()

    spawn = spawn_skill_chat(
        caller, agent_id, chat_name="assist-abc123", message="/assist it broke", probe=_READY_PROBE
    )

    assert spawn.is_started is True
    assert spawn.failure_detail == ""
    assert caller.calls == [_script_call(agent_id, chat_name="assist-abc123", message="/assist it broke")]


def test_a_spawn_the_chat_app_refused_is_final_in_the_chat_apps_words() -> None:
    """A refusal names a chat already made or a workspace that cannot make one; a bare create
    behind it would make a second chat or fail the same way."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=script_exit_stdout(
                1,
                inner_stderr=(
                    "Falling back to nothing\nThe chat app did not create the chat: mngr create exited with code 1\n"
                ),
            ),
        )
    )

    spawn = spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist it broke", probe=_READY_PROBE)

    assert spawn.is_started is False
    assert spawn.failure_detail == "The chat app did not create the chat: mngr create exited with code 1"
    assert len(caller.calls) == 1


def test_a_script_without_a_create_mode_falls_back_to_the_bare_create() -> None:
    """A template with the messaging script but not yet its create mode answers argparse's 2,
    which is no verdict on the chat: the app's own create runs, bound the way it was before."""
    caller = ScriptedMngrCaller(results=(_SCRIPT_NO_VERDICT, MngrCallResult(returncode=0)))
    agent_id = AgentId.generate()

    spawn = spawn_skill_chat(
        caller, agent_id, chat_name="assist-abc123", message="/assist it broke", probe=_READY_PROBE
    )

    assert spawn.is_started is True
    assert caller.calls == [
        _script_call(agent_id, chat_name="assist-abc123", message="/assist it broke"),
        build_skill_chat_mngr_args(agent_id, chat_name="assist-abc123", message="/assist it broke"),
    ]


def test_the_chat_apps_create_waits_longer_than_the_bare_create_behind_it() -> None:
    """The chat app holds its answer until the create has finished, so a ceiling no higher than
    the bare create's would always expire first: the user would get minds' silence about a run
    it never heard back from, while the chat goes on being made and opens its window moments later."""
    caller = ScriptedMngrCaller(results=(_SCRIPT_NO_VERDICT, MngrCallResult(returncode=0)))

    spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist it broke", probe=_READY_PROBE)

    through_chat_app, bare_create = caller.recorded_calls
    assert through_chat_app.timeout is not None and bare_create.timeout is not None
    assert through_chat_app.timeout > bare_create.timeout


def test_the_bare_create_fallback_binds_a_legacy_workspace_through_its_resolver() -> None:
    """Only the fallback asks the resolver: a chat the chat app makes is bound by the chat app."""
    legacy_probe = SkillProbe(support=SkillSupport.SUPPORTED, is_local_settings_present=False)
    caller = ScriptedMngrCaller(
        results=(
            _SCRIPT_NO_VERDICT,
            MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="/home/user/acc/a1")),
            MngrCallResult(returncode=0),
        )
    )
    agent_id = AgentId.generate()

    spawn = spawn_skill_chat(caller, agent_id, chat_name="x", message="/update-self", probe=legacy_probe)

    assert spawn.is_started is True
    assert caller.calls[1] == build_account_binding_probe_args(agent_id)
    assert caller.calls[2] == build_skill_chat_mngr_args(
        agent_id, chat_name="x", message="/update-self", account_args=("--env", "CLAUDE_CONFIG_DIR=/home/user/acc/a1")
    )


def test_the_bare_create_runs_a_chat_create_inside_the_workspace_with_the_seed_message() -> None:
    agent_id = AgentId.generate()
    args = build_skill_chat_mngr_args(agent_id, chat_name="assist-abc123", message="/assist it broke")
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


def test_a_failed_bare_create_carries_the_machines_own_refusal() -> None:
    """The caller renders this; without it the user is told only to try again."""
    stderr = (
        "WARNING: outer SSH unreachable for host host-other: Host not found: host-other\n"
        "Error: Unknown fields in agent_types.opencode: ['auto_allow_permissions']\n"
        "ERROR: Command failed on agent system-services\n"
    )
    caller = ScriptedMngrCaller(
        results=(_SCRIPT_NO_VERDICT, MngrCallResult(returncode=1, stderr=stderr, is_mngr_output=True))
    )

    spawn = spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist it broke", probe=_READY_PROBE)

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
    """These stderrs are minds' own lines, so showing them as the machine's verdict misattributes our own fault.

    No answer is also no verdict from the script, but not the kind a bare create may follow:
    a timed-out create may well have made the chat."""
    caller = RecordingMngrCaller(result=result)

    spawn = spawn_skill_chat(
        caller, AgentId.generate(), chat_name="x", message="/assist my laptop", probe=_READY_PROBE
    )

    assert spawn.is_started is False
    assert spawn.failure_detail == ""
    assert len(caller.calls) == 1


def test_a_spawn_the_exec_could_not_run_carries_the_reason_mngr_gave_not_its_chatter() -> None:
    """``--format jsonl`` puts the exec's own refusal in an event on stdout and leaves stderr to
    the outer mngr's discovery chatter, which would otherwise be shown as the machine's verdict."""
    caller = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=1,
            stdout=exec_error_stdout("Agent 123 is not running (state: STOPPED)"),
            stderr="WARNING: outer SSH unreachable for host host-other: Host not found: host-other\n",
            is_mngr_output=True,
        )
    )

    spawn = spawn_skill_chat(caller, AgentId.generate(), chat_name="x", message="/assist it broke", probe=_READY_PROBE)

    assert spawn.is_started is False
    assert spawn.failure_detail == "Agent 123 is not running (state: STOPPED)"
    assert len(caller.calls) == 1


# CLEANUP: the resolver tests below go with the resolver (see skill_chat.py).


def test_a_workspace_that_writes_its_create_defaults_is_not_asked_for_an_account() -> None:
    """Its own mngr binds the create, so the app has no question to ask and makes no call."""
    caller = RecordingMngrCaller()
    probe = SkillProbe(support=SkillSupport.SUPPORTED, is_local_settings_present=True)

    assert resolve_legacy_account_args(caller, AgentId.generate(), probe) == ()
    assert caller.calls == []


def test_a_workspace_without_the_file_is_bound_through_its_own_resolver() -> None:
    caller = RecordingMngrCaller(
        result=MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="/home/user/acc/a1"))
    )
    probe = SkillProbe(support=SkillSupport.SUPPORTED, is_local_settings_present=False)

    args = resolve_legacy_account_args(caller, AgentId.generate(), probe)

    assert args == ("--env", "CLAUDE_CONFIG_DIR=/home/user/acc/a1")
    assert len(caller.calls) == 1


@pytest.mark.parametrize(
    "result",
    (
        MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir="")),
        MngrCallResult(returncode=1, stderr="connection refused"),
        MngrCallResult(returncode=0, stdout=account_binding_probe_stdout(account_dir=None)),
    ),
    ids=("resolves-none", "cannot-be-asked", "keeps-no-accounts"),
)
def test_a_workspace_the_resolver_cannot_bind_gets_a_bare_create_rather_than_a_refusal(result: MngrCallResult) -> None:
    """What the workspace makes of a bare create is the verdict the user sees, in the workspace's own words."""
    caller = RecordingMngrCaller(result=result)
    probe = SkillProbe(support=SkillSupport.SUPPORTED, is_local_settings_present=False)

    assert resolve_legacy_account_args(caller, AgentId.generate(), probe) == ()


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
