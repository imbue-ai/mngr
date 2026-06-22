"""Tests for Modal agent creation from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block. This makes it
easy to maintain the mapping between tutorial content and test coverage via the
tutorial_matcher script.
"""

import json
import re

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.mngr.utils.polling import wait_for
from imbue.skitwright.expect import expect

_REMOTE_TIMEOUT = 120.0
# test_create_modal_build_args uses a custom image (-b image=python:3.12) that
# has to pull the image and apt-install openssh/tmux/rsync/jq/xxd at runtime,
# which pushes the total past the default _REMOTE_TIMEOUT. Bumping just this
# test's wait rather than all of them keeps the common case tight.
_REMOTE_TIMEOUT_CUSTOM_IMAGE = 240.0


# All tests in this file invoke the Modal CLI indirectly (via environment_create
# during provider initialization), so they need @pytest.mark.modal to satisfy
# the resource guard.
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also launch your default agent remotely in Modal:
        mngr create my-task --provider modal
        # see more details below in "CREATING AGENTS REMOTELY" for relevant options
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --no-connect --no-ensure-clean",
        comment="you can also launch your default agent remotely in Modal",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the agent was actually created on the Modal provider (not just that
    # the command exited 0). Filtering the listing by --provider modal means a
    # match inherently confirms the agent lives on a Modal-backed host.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the agent is running on the modal provider",
    )
    expect(list_result).to_succeed()
    agents_by_name = {agent["name"]: agent for agent in json.loads(list_result.stdout)["agents"]}
    assert "my-task" in agents_by_name, f"Expected 'my-task' among modal agents, got: {list(agents_by_name)}"
    # Tie the agent to its host so we confirm *that* agent is genuinely hosted on
    # Modal (rather than relying solely on the --provider modal listing filter),
    # and that a real Modal host was actually provisioned and is running.
    my_task = agents_by_name["my-task"]
    assert my_task["host"]["provider_name"] == "modal", (
        f"Expected my-task on the modal provider, got: {my_task['host']['provider_name']!r}"
    )
    assert my_task["host"]["state"] == "RUNNING", (
        f"Expected the modal host to be RUNNING, got: {my_task['host']['state']!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(660)
def test_create_modal_no_connect_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
    mngr create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github"
    # here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
    # and then we also pass in an explicit message for the agent to start working on immediately
    # the message can also be specified as the contents of a file (by using --message-file instead of --message)
    """)
    # Use a generous ready timeout because the agent needs to fully start
    # (install Claude Code, authenticate, signal readiness) before the message
    # can be sent. This is slow on fresh Modal hosts (~2-5 min), and even
    # slower in Modal-in-Modal (offload) environments (~5-8 min).
    result = e2e.run(
        # --type claude is required since mngr no longer source-codes a default
        # agent type (it now lives in user config, which the e2e profile does
        # not set). The message-delivery path being exercised here needs a real
        # Claude Code agent that authenticates with the passed ANTHROPIC_API_KEY.
        'MNGR__AGENT_READY_TIMEOUT=540 mngr create my-task --provider modal --type claude --no-connect --pass-env ANTHROPIC_API_KEY --message "Speed up one of my tests and make a PR on github" --no-ensure-clean',
        comment="you can send an initial message (so you don't have to wait around)",
        timeout=600.0,
    )
    if result.exit_code != 0:
        diagnostics = e2e.collect_remote_diagnostics("my-task")
        raise AssertionError(
            f"Expected command to succeed but got exit code {result.exit_code}\n"
            f"  Command: {result.command}\n"
            f"  Stderr:\n    {result.stderr}\n"
            f"{diagnostics}"
        )

    # The behavior that distinguishes this command from a plain remote create is
    # the initial-message delivery (``--message``). Confirm that path actually
    # ran rather than only checking the exit code -- create logs this progress
    # line to stderr right before it sends the message to the started agent.
    assert "Sending initial message" in result.stderr, (
        f"Expected the create output to show the initial message being sent, got:\n{result.stderr}"
    )

    # The create-side log line above only proves mngr *attempted* to send the
    # message. Confirm the agent-side effect too: the agent actually received the
    # prompt. claude records the submitted prompt to its session log, which the
    # agent's background converter turns into a ``user_message`` event, so the
    # user-facing ``mngr transcript --role user`` surfaces it. The conversion runs
    # asynchronously on the remote host shortly after submission, so poll until
    # the message shows up (and tolerate the transient "no transcript yet" error
    # that mngr returns before the first event is written).
    def _initial_message_received() -> bool:
        transcript_result = e2e.run(
            "mngr transcript my-task --role user",
            comment="confirm the agent actually received the initial message",
            timeout=_REMOTE_TIMEOUT,
        )
        return transcript_result.exit_code == 0 and "Speed up one of my tests" in transcript_result.stdout

    wait_for(
        _initial_message_received,
        timeout=180.0,
        poll_interval=5.0,
        error_message="The initial message never appeared in the agent's transcript (--role user).",
    )

    # Verify the concrete effect of the command: the agent was actually created
    # and is running on the Modal provider. Filtering the listing by --provider
    # modal means a match inherently confirms the agent lives on a Modal-backed
    # host (not just that the create command exited 0).
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the agent is running on the modal provider",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    modal_agent_names = [agent["name"] for agent in json.loads(list_result.stdout)["agents"]]
    assert "my-task" in modal_agent_names, f"Expected 'my-task' among modal agents, got: {modal_agent_names}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_edit_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mngr create my-task --provider modal --edit-message
    """)
    # --edit-message opens $EDITOR (defaulting to vim) to compose the message.
    # The e2e environment has no interactive editor installed, so point EDITOR
    # at `true`: it exits 0 immediately, leaving the (empty) message buffer
    # untouched. This exercises the full --edit-message flow (editor launches in
    # parallel with creation, the create path waits for and handles the editor
    # exit) without sending a message, which would otherwise require the remote
    # agent to be fully ready -- the 120s timeout intentionally does not wait
    # for that.
    result = e2e.run(
        "EDITOR=true mngr create my-task --provider modal --edit-message --no-connect --no-ensure-clean",
        comment="you can also edit the message *while the agent is starting up*",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the --edit-message path actually ran rather than just trusting the
    # exit code. With EDITOR=true the editor opens and closes immediately with an
    # empty buffer, so the post-create message-send handling must report that
    # there was nothing to send. This message only appears when the editor was
    # launched in parallel with creation and its (empty) exit was handled -- i.e.
    # the full --edit-message flow was exercised.
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_match(r"(?i)no message to send")
    # ...and conversely, since the (empty) editor buffer means there is nothing to
    # deliver, create must NOT have entered its message-send path. The "Sending
    # initial message" progress line is what create logs right before it delivers a
    # message (see test_create_modal_no_connect_message), so its absence confirms
    # the empty edit-message buffer short-circuited delivery rather than sending an
    # empty message.
    assert "Sending initial message" not in combined_output, (
        f"Expected no message to be sent for an empty edit-message buffer, got:\n{combined_output}"
    )

    # Verify an agent was genuinely created on the Modal provider (not just that
    # the command exited 0). Filtering the listing by --provider modal means a
    # match inherently confirms the agent lives on a Modal-backed host.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the agent was created on the modal provider",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    modal_agent_names = [agent["name"] for agent in json.loads(list_result.stdout)["agents"]]
    assert "my-task" in modal_agent_names, f"Expected 'my-task' among modal agents, got: {modal_agent_names}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
# Bumped past the usual 120 because this test also does a `mngr exec` round-trip
# (on top of remote host creation) to verify the rsync transfer actually landed.
@pytest.mark.timeout(180)
def test_create_modal_rsync(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    # Seed the source repo with two *gitignored* files so we can prove the actual
    # effect of the flags, not just a 0 exit code. mngr's git transfer carries
    # committed and unclean *tracked* files but not gitignored ones, so only the
    # supplemental rsync pass (enabled by --rsync) can deliver these. The pass
    # copies data/extra.txt -- demonstrating "extra data beyond just the git
    # data" -- while honoring --rsync-args "--exclude=node_modules", so the
    # node_modules marker must NOT arrive on the host.
    e2e.run(
        "printf 'node_modules/\\ndata/\\n' >> .gitignore"
        " && mkdir -p data node_modules"
        " && echo rsync-extra-data-marker > data/extra.txt"
        " && echo should-be-excluded > node_modules/marker.txt",
        comment="seed gitignored extra data and an excluded node_modules dir",
    )
    result = e2e.run(
        'mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules" --no-connect --no-ensure-clean',
        comment="you can use rsync to transfer extra data as well",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the concrete effect on the host: the gitignored data file rode along
    # via rsync (proving rsync moved data beyond the git contents), while
    # node_modules was excluded by --rsync-args. exec runs in the agent work dir,
    # so the paths are relative to it.
    verify = e2e.run(
        "mngr exec my-task 'cat data/extra.txt; test ! -e node_modules && echo NODE_MODULES_EXCLUDED'",
        comment="verify rsync transferred the extra data and excluded node_modules",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(verify).to_succeed()
    expect(verify.stdout).to_contain("rsync-extra-data-marker")
    expect(verify.stdout).to_contain("NODE_MODULES_EXCLUDED")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_passthrough_agent_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # one of the coolest features of mngr is the ability to create agents on remote hosts just as easily as you can create them locally:
    mngr create my-task --provider modal -- --dangerously-skip-permissions --append-system-prompt "Don't ask me any questions!"
    # that command passes the "--dangerously-skip-permissions" flag to claude because it's safe to do so:
    # agents running remotely are running in a sandboxed environment where they can't really mess anything up on their local machine (or if they do, it doesn't matter)
    # because it's running remotely, you might also want something like that system prompt (to tell it not to get blocked on you)
    """)
    result = e2e.run(
        'mngr create my-task --provider modal --no-connect --no-ensure-clean -- --dangerously-skip-permissions --append-system-prompt "Don\'t ask me any questions!"',
        comment="one of the coolest features of mngr is the ability to create agents on remote hosts",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # The point of this tutorial block is that the args after `--` are passed
    # through to the (claude) agent, not consumed by mngr. Verify they actually
    # made it into the agent's assembled launch command rather than just trusting
    # the exit code.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the passthrough agent args reached the agent",
    )
    expect(list_result).to_succeed()
    matching = [agent for agent in json.loads(list_result.stdout)["agents"] if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {matching}"
    agent = matching[0]
    # The tutorial block specifically passes these flags "to claude", so confirm
    # the passthrough landed on a real claude agent (not some other agent type).
    assert agent["type"] == "claude", f"expected a claude agent, got: {agent['type']}"
    command = agent["command"]
    assert "--dangerously-skip-permissions" in command, command
    assert "--append-system-prompt" in command, command
    # The system prompt text survives shlex-quoting (only the apostrophe is escaped).
    assert "ask me any questions" in command, command

    # The args weren't just recorded -- they reached an agent on a genuinely
    # provisioned, running Modal host (create blocks until the host is up and the
    # agent is started). This rules out the passthrough surviving only in metadata
    # while host provisioning silently failed.
    assert agent["host"]["provider_name"] == "modal", agent["host"]
    assert agent["host"]["state"] == "RUNNING", (
        f"expected the modal host to be RUNNING, got: {agent['host']['state']!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_idle_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
    # one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
    # mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
    mngr create my-task --provider modal --idle-timeout 60
    # that command shuts down the Modal host (and agent) after 1 minute of inactivity.
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --idle-timeout 60 --no-connect --no-ensure-clean",
        comment="mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the concrete effect of --idle-timeout 60: the created Modal host
    # is configured to shut down after 60 seconds of inactivity. The listing
    # surfaces this as the agent's idle_timeout_seconds field.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="that command shuts down the Modal host (and agent) after 1 minute of inactivity",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [agent for agent in parsed["agents"] if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {parsed['agents']}"
    assert matching[0]["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds=60, got: {matching[0]['idle_timeout_seconds']}"
    )


# No @pytest.mark.modal here: an invalid --idle-timeout value is rejected while
# parsing the create options, before any Modal API call is made (mirroring
# test_create_address_syntax_existing_host), so the modal resource guard would
# fire ("marked modal but never invoked modal") if the marker were present.
@pytest.mark.release
@pytest.mark.timeout(120)
def test_create_modal_idle_timeout_invalid(e2e: E2eSession) -> None:
    # Unhappy-path companion to test_create_modal_idle_timeout: the same tutorial
    # block, but exercising what happens when the user passes a value that is not
    # a valid duration. This must be rejected with a clear error rather than
    # silently provisioning a host with a bogus (or default) idle timeout.
    e2e.write_tutorial_block("""
    # running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
    # one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
    # mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
    mngr create my-task --provider modal --idle-timeout 60
    # that command shuts down the Modal host (and agent) after 1 minute of inactivity.
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --idle-timeout not-a-duration --no-connect --no-ensure-clean",
        comment="an invalid --idle-timeout value is rejected before any host is provisioned",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_fail()
    # The error must name the offending value and explain it is an invalid
    # duration (this is the UserInputError raised by parse_duration_to_seconds).
    # The value is rejected while parsing the create options, before any host is
    # provisioned, so a clear message -- rather than a silently-created host with
    # a bogus/default timeout -- is the behavior under test.
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)invalid duration")
    expect(combined).to_contain("not-a-duration")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_idle_mode_ssh(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # You can customize what "inactivity" means by using the --idle-mode flag:
    mngr create my-task --provider modal --idle-mode "ssh"
    # that command will only consider agents as "idle" when you are not connected to them
    # see the idle_detection.md file for more details on idle detection and timeouts
    """)
    # The tutorial relies on the user's configured default agent type; the
    # isolated test profile has none, so pass an explicit --type. "ssh" idle
    # detection is independent of the agent type, so a lightweight command
    # agent exercises the same code path (matching test_create_modal_idle_mode_run).
    result = e2e.run(
        'mngr create my-task --provider modal --idle-mode "ssh" --type command --no-connect --no-ensure-clean -- sleep 100981',
        comment="You can customize what inactivity means by using the --idle-mode flag",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the concrete effect of --idle-mode "ssh": the created command agent
    # really exists, runs our command, and carries the requested idle mode. The
    # non-terminating sleep keeps the host online so the listing surfaces
    # idle_mode (it is only reported while the host is up). idle_timeout is left
    # at its default here, so we only assert on the idle_mode the flag set.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="that command will only consider agents as idle when you are not connected to them",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    agent = matching[0]
    assert agent["type"] == "command", f"Expected a command-type agent, got: {agent['type']}"
    assert "sleep 100981" in agent["command"], f"Expected the sleep command, got: {agent['command']}"
    assert agent["idle_mode"] is not None and agent["idle_mode"].lower() == "ssh", (
        f"Expected idle_mode 'ssh', got: {agent['idle_mode']}"
    )


# No @pytest.mark.modal here: resolving the address against a non-existent named
# host (no .PROVIDER suffix) fails during host lookup before any Modal API call,
# so the modal resource guard would fire ("marked modal but never invoked modal").
@pytest.mark.release
@pytest.mark.timeout(120)
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    # --type is supplied explicitly (the tutorial assumes the user has a default
    # agent type configured) so the command gets past agent-type resolution and
    # reaches host resolution -- which is the behavior this test exercises.
    # `my-dev-box` is not a registered host, so resolution must fail.
    result = e2e.run(
        "mngr create my-task@my-dev-box --type claude --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_fail()
    # The error should mention the host not being found
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)host.*not found|no.*host|unknown.*host|could not find.*host|not.*registered")
    # The exact host name parsed from the address must surface in the error,
    # proving the `name@host` address was parsed correctly (host == my-dev-box)
    # and that this specific host is what could not be resolved.
    expect(combined).to_contain("my-dev-box")

    # The failed create must not leave a half-created agent behind. Host
    # resolution fails before any agent is registered, so listing the local
    # provider (the only one that could hold a leaked agent in this fresh env)
    # must show no `my-task` agent -- confirming the failure was clean rather
    # than only checking the error message.
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="confirm the failed create left no orphan agent behind",
    )
    expect(list_result).to_succeed()
    leaked = [agent["name"] for agent in json.loads(list_result.stdout)["agents"] if agent["name"] == "my-task"]
    assert not leaked, f"Expected no 'my-task' agent after a failed create, found: {leaked}"


@pytest.mark.release
@pytest.mark.tmux
# No @pytest.mark.modal here: this happy-path variant creates only local
# (`--type command`) agents and scopes every listing to --provider local, so the
# modal CLI is never invoked and the modal resource guard would fire if marked.
# No @pytest.mark.rsync either: local creates default to the git-mirror transfer
# mode, which does not shell out to rsync unless `--rsync` is passed (it is not).
@pytest.mark.timeout(120)
def test_create_address_syntax_targets_existing_host(e2e: E2eSession) -> None:
    # Shares the tutorial block with test_create_address_syntax_existing_host but
    # exercises the happy path: the `name@host` address resolves to a host that
    # actually exists, so the agent is placed on it (rather than failing lookup).
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    # First create a local agent so a real host exists to target. All local
    # agents share the single `localhost` host, which is what the address syntax
    # below resolves against. `--type command -- sleep` stands in for the
    # tutorial's default (claude) agent so no claude install / API key is needed.
    setup = e2e.run(
        "mngr create host-owner --type command --no-ensure-clean --no-connect -- sleep 100322",
        comment="create an initial agent so an existing host is available to target",
    )
    expect(setup).to_succeed()

    # Discover the existing host's name from the listing rather than hardcoding
    # it, so the test targets whatever host mngr actually registered.
    pre_list = e2e.run(
        "mngr list --provider local --format json",
        comment="discover the existing host to target",
    )
    expect(pre_list).to_succeed()
    owner_agent = {a["name"]: a for a in json.loads(pre_list.stdout)["agents"]}["host-owner"]
    existing_host = owner_agent["host"]["name"]

    # Create a second agent that targets that existing host via the `name@host`
    # address syntax -- this is the behavior the tutorial block demonstrates.
    result = e2e.run(
        f"mngr create my-task@{existing_host} --type command --no-ensure-clean --no-connect -- sleep 100323",
        comment="you can specify which existing host to run on using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the address actually placed the agent on the targeted existing host:
    # my-task must land on the *same* host as host-owner (a real reuse of the
    # existing host), not a freshly provisioned one. Comparing host ids proves
    # the `name@host` address resolved to the existing host.
    post_list = e2e.run(
        "mngr list --provider local --format json",
        comment="confirm the agent landed on the targeted existing host",
    )
    expect(post_list).to_succeed()
    agents_by_name = {a["name"]: a for a in json.loads(post_list.stdout)["agents"]}
    assert "my-task" in agents_by_name, f"my-task not found in agents: {list(agents_by_name)}"
    assert agents_by_name["my-task"]["host"]["id"] == owner_agent["host"]["id"], (
        "Expected my-task to share host-owner's existing host, got "
        f"{agents_by_name['my-task']['host']['id']} vs {owner_agent['host']['id']}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_build_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # generally though, you'll want to construct a new Modal host for each agent.
    # build arguments let you customize that new remote host (eg, GPU type, memory, base Docker image for Modal):
    mngr create my-task --provider modal -b cpu=4 -b memory=16 -b image=python:3.12
    # see "mngr create --help" for all provider-specific build args
    # some other useful Modal build args: --region, --timeout, --offline (blocks network), --secret, --cidr-allowlist, --context-dir
    """)
    # The tutorial block relies on the user's configured default agent type; the
    # isolated test profile has none, so pin a lightweight `--type command -- sleep`
    # agent (needs no claude install or API key) to exercise the build args without
    # provisioning a real coding agent. The build args under test (image, cpu,
    # memory) are independent of the agent type, and a command agent keeps the
    # host up so the exec/list verification below works -- mirroring the sibling
    # custom-build tests (test_create_modal_custom_dockerfile_only,
    # test_create_modal_dockerfile_and_context).
    result = e2e.run(
        "mngr create my-task --provider modal --type command -b cpu=4 -b memory=16 -b image=python:3.12"
        " --no-connect --no-ensure-clean -- sleep 100301",
        comment="build arguments let you customize that new remote host",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_succeed()

    # Verify the build args had a real effect, not just that the command exited 0.
    # The `-b image=python:3.12` arg should make python 3.12 the host interpreter,
    # so `python --version` on the host must report 3.12.x. This distinguishes a
    # genuinely-customized host from the default mngr base image.
    version_result = e2e.run(
        "mngr exec my-task 'python --version'",
        comment="verify the custom python:3.12 base image is in use on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(version_result).to_succeed()
    expect(version_result.stdout + version_result.stderr).to_match(r"Python 3\.12\.")

    # The command also passes -b cpu=4 and -b memory=16, so verify those build
    # args took effect too (not just the image). Modal does not expose
    # cpu/memory limits inside the container, so assert on the resources mngr
    # recorded for the host (the same approach as test_create_modal_cpu_memory_gpu):
    # cpu.count renders as an int (4); memory_gb as a float (16.0).
    resource_result = e2e.run(
        "mngr list --provider modal --include 'name == \"my-task\"' "
        "--format 'RES:{name}|{host.resource.cpu.count}|{host.resource.memory_gb}'",
        comment="confirm the host was created with the requested cpu and memory build args",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(resource_result).to_succeed()
    expect(resource_result.stdout).to_contain("RES:my-task|4|16.0")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_dockerfile_and_context(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # the most important build args for Modal are probably "--file" and "--context-dir",
    # which let you specify a custom Dockerfile and build context directory (respectively) for building the host environment.
    # This is how you can get custom dependencies, files, and setup steps on your Modal hosts. For example:
    mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context
    # that command builds a Modal host using the Dockerfile at ./Dockerfile.agent and the build context at ./agent-context
    # (which is where the Dockerfile can COPY files from, and also where build args are evaluated from)
    """)
    # Build a context directory containing a marker file, plus a Dockerfile that
    # COPYs that marker out of the context into the image. This exercises *both*
    # build args together exactly as the tutorial describes them: --file selects
    # the custom Dockerfile, and --context-dir is "where the Dockerfile can COPY
    # files from". Reading the marker back off the running host (below) proves the
    # host was really built from this Dockerfile + context, not the default image.
    e2e.run(
        "mkdir -p agent-context"
        " && echo 'dockerfile-context-marker' > agent-context/marker.txt"
        " && printf 'FROM python:3.12-slim\\nCOPY marker.txt /opt/marker.txt\\n' > Dockerfile.agent",
        comment="create Dockerfile and context",
    )
    # The tutorial block relies on the user's configured default agent type; the
    # isolated test profile has none, so pin a lightweight `--type command -- sleep`
    # agent (needs no claude install or API key) to exercise the custom build
    # without provisioning a real coding agent. Mirrors
    # test_create_modal_custom_dockerfile_only.
    result = e2e.run(
        "mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context"
        " --type command --no-connect --no-ensure-clean -- sleep 100300",
        comment="the most important build args for Modal are --file and --context-dir",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_succeed()

    # Verify the host actually runs the custom image built from our Dockerfile and
    # context: the marker COPYed from ./agent-context must be present on the host.
    # This fails if the build silently fell back to the default base image or
    # ignored --context-dir.
    exec_result = e2e.run(
        "mngr exec my-task 'cat /opt/marker.txt'",
        comment="confirm the custom Dockerfile + context-dir build is in use on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("dockerfile-context-marker")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_named_host_new_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can name the host using the address syntax:
    mngr create my-task@my-modal-box.modal --new-host
    # (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)
    """)
    # The tutorial block relies on the user's default agent type (claude). The
    # isolated e2e profile configures no default, so substitute a lightweight
    # `--type command -- sleep` agent (the same pattern used by
    # test_create_modal_idle_mode_run and the other tutorial tests). The point
    # of this test is the `@host.modal --new-host` host-naming address syntax,
    # for which the agent type is incidental.
    result = e2e.run(
        "mngr create my-task@my-modal-box.modal --new-host --type command --no-connect --no-ensure-clean -- sleep 100980",
        comment="you can name the host using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the address syntax was actually honored: the agent must land on a
    # host explicitly named `my-modal-box` under the modal provider, not an
    # auto-generated host name. `mngr list --addrs` prints each agent's
    # `name@host.name.provider` address, so the created agent should appear
    # verbatim as `my-task@my-modal-box.modal`. Scope the listing to the modal
    # provider so it does not fan out to other (unconfigured) providers such as
    # AWS, whose discovery errors would make `mngr list` exit non-zero in the
    # test environment even though the modal listing itself is correct.
    listing = e2e.run("mngr list --provider modal --addrs", comment="verify the named host was created")
    expect(listing).to_succeed()
    expect(listing.stdout).to_contain("my-task@my-modal-box.modal")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
# This test does a remote create plus an exec round-trip, and the create's final
# "initial snapshot" step is slow and variable in the Modal-in-Modal (offload)
# environment, so the overall timeout is generous (matching
# test_create_modal_idle_timeout_60).
@pytest.mark.timeout(420)
def test_create_modal_volume(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
    mngr create my-task --provider modal -b volume=my-data:/data
    """)
    # The tutorial relies on the user's configured default agent type; the
    # isolated e2e environment configures none, so pass an explicit lightweight
    # `command` agent (matching the convention in the rest of the e2e suite).
    # The volume mount behavior under test is identical regardless of agent type.
    result = e2e.run(
        "mngr create my-task --provider modal -b volume=my-data:/data --type command --no-connect --no-ensure-clean -- sleep 100985",
        comment="you can mount persistent Modal volumes",
        # The create's final "initial snapshot" step is slow and variable in the
        # Modal-in-Modal (offload) environment, so give it a more generous timeout
        # than the default _REMOTE_TIMEOUT to avoid flaky timeouts.
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_succeed()

    # Verify the actual effect of -b volume=my-data:/data: the volume mount
    # point /data exists on the host and is writable. Writing then reading back
    # a sentinel through /data proves the mount is functional, not just that the
    # flag was accepted at the CLI.
    verify = e2e.run(
        "mngr exec my-task 'echo mngr-volume-probe > /data/probe.txt && cat /data/probe.txt'",
        comment="verify the volume is mounted and writable at /data",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(verify).to_succeed()
    expect(verify.stdout).to_contain("mngr-volume-probe")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
# Bumped past the usual 120 because, on top of remote host creation, this test
# also does a `mngr list` and a `mngr exec` round-trip to verify the work dir.
@pytest.mark.timeout(180)
def test_create_modal_target_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the target path where the agent's work directory will be mounted:
    mngr create my-task@.modal:/workspace
    """)
    # --type command -- sleep <N> stands in for the default (claude) agent so
    # the test doesn't need claude installed/authenticated on the remote host;
    # the target-path behavior under test is independent of the agent type.
    result = e2e.run(
        "mngr create my-task@.modal:/workspace --type command --no-connect --no-ensure-clean -- sleep 100200",
        comment="you can specify the target path where the agent's work directory will be mounted",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the target path was actually honored, not just that create exited 0.
    # mngr's own metadata should record the requested mount point as the work dir...
    # Scope the listing to the modal provider: a bare ``mngr list`` enumerates
    # every enabled backend and (by default, ``--on-error abort``) fails loudly if
    # any is unavailable -- e.g. the AWS backend with no credentials configured in
    # the isolated e2e environment. The agent under test lives on modal, so
    # ``--provider modal`` keeps discovery targeted (the documented way to avoid an
    # unrelated provider aborting the listing) and matches the rest of this file.
    list_result = e2e.run(
        "mngr list --provider modal --format json", comment="inspect the agent's recorded work directory"
    )
    expect(list_result).to_succeed()
    agents_by_name = {a["name"]: a for a in json.loads(list_result.stdout)["agents"]}
    assert "my-task" in agents_by_name, f"my-task not found in agents: {list(agents_by_name)}"
    assert agents_by_name["my-task"]["work_dir"] == "/workspace", (
        f"Expected work_dir '/workspace', got: {agents_by_name['my-task']['work_dir']!r}"
    )

    # ...and the directory should really be where the agent runs on the remote
    # host, with the transferred git repository actually mounted there (proving
    # the *work directory* -- not merely the shell cwd string -- landed at the
    # target path). The single exec keeps remote round-trips down: it prints the
    # agent's pwd and, only if /workspace contains the mounted repo's .git dir,
    # a sentinel. mngr exec appends a "Command succeeded on agent ..." status
    # line, so assert on the command's own output lines.
    pwd_result = e2e.run(
        "mngr exec my-task 'pwd && test -d /workspace/.git && echo WORKDIR_REPO_MOUNTED'",
        comment="confirm the agent runs in the target path on the host",
    )
    expect(pwd_result).to_succeed()
    pwd_output_lines = pwd_result.stdout.splitlines()
    assert "/workspace" in pwd_output_lines, (
        f"Expected agent pwd '/workspace' in exec output, got: {pwd_result.stdout!r}"
    )
    assert "WORKDIR_REPO_MOUNTED" in pwd_output_lines, (
        f"Expected the git repo to be mounted at the target path /workspace, got: {pwd_result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_upload_and_extra_provision_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can upload files and run custom commands during host provisioning:
        mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo"
        # (provision commands run as the host's default user; prefix with sudo to run as root if it is not)
    """)
    # `pip install foo` from the tutorial would fail at provision time (no such
    # package), so the test substitutes a harmless command but still exercises
    # the upload-file + extra-provision-command flags end to end.
    #
    # The tutorial block omits an explicit agent type (relying on the user's
    # configured default, claude). Substitute a lightweight `--type command --
    # sleep` agent (the convention used by the rest of this file): the
    # upload-file + extra-provision-command behavior runs during host
    # provisioning and is independent of the agent type, so this exercises the
    # same code path without the slow claude install (and its "no API
    # credentials" warning) the default type would trigger.
    #
    # Seed the source file with a sentinel and have the provision command drop a
    # marker file so we can verify both flags actually took effect on the remote
    # host (rather than only asserting the command exited 0). The sentinel must
    # be a valid SSH config line because the upload target (/root/.ssh/config) is
    # the host's real SSH client config -- a comment line is parsed-and-ignored
    # by every SSH config reader, so it is safe while still being verifiable. The
    # provision command also records the user it ran as ($(whoami)) so we can
    # verify the tutorial's documented "runs as the host's default user" claim.
    e2e.run(
        "mkdir -p ~/.ssh && echo '# upload-sentinel-12345' > ~/.ssh/config",
        comment="create ssh config for upload test",
    )
    result = e2e.run(
        "mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config"
        ' --extra-provision-command "echo provision-marker-67890 ran-as-$(whoami) > /tmp/mngr_provision_marker"'
        " --type command --no-connect --no-ensure-clean -- sleep 100563",
        comment="you can upload files and run custom commands during host provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # The --upload-file flag should have placed the local file (with our
    # sentinel contents) at the requested path on the remote host.
    uploaded = e2e.run(
        "mngr exec my-task 'cat /root/.ssh/config'",
        comment="verify the uploaded file landed on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(uploaded).to_succeed()
    expect(uploaded.stdout).to_contain("upload-sentinel-12345")

    # The --extra-provision-command should have run during provisioning, leaving
    # the marker file it wrote behind on the host.
    provisioned = e2e.run(
        "mngr exec my-task 'cat /tmp/mngr_provision_marker'",
        comment="verify the extra-provision-command ran on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(provisioned).to_succeed()
    expect(provisioned.stdout).to_contain("provision-marker-67890")

    # Verify the tutorial's documented behavior that "provision commands run as
    # the host's default user": the user captured in the marker during
    # provisioning must match the user the agent's own commands run as on the
    # host. Comparing against the live `whoami` (rather than hardcoding "root")
    # keeps the assertion robust to the provider's default-user choice.
    whoami_result = e2e.run(
        "mngr exec my-task whoami",
        comment="determine the host's default user the agent runs as",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(whoami_result).to_succeed()
    # exec appends a "Command succeeded on agent ..." status line, so pick the
    # username out of the remaining (command) output.
    whoami_lines = [
        line.strip()
        for line in whoami_result.stdout.splitlines()
        if line.strip() and "Command succeeded" not in line
    ]
    assert whoami_lines, f"Expected whoami output, got: {whoami_result.stdout!r}"
    host_user = whoami_lines[0]
    assert f"ran-as-{host_user}" in provisioned.stdout, (
        f"Expected the provision command to have run as the host's default user {host_user!r}, "
        f"but the marker recorded: {provisioned.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_no_start_on_boot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, agents are started when a host is booted. This can be disabled:
    mngr create my-task --provider modal --no-start-on-boot
    # but it only makes sense to do this if you are running multiple agents on the same host
    # that's because hosts are automatically stopped when they have no more running agents, so you have to have at least one.
    """)
    # The tutorial block omits an explicit agent type (relying on the user's
    # configured default). The test env has no default type, so substitute the
    # lightweight `command` type (with a stand-in `sleep` command), matching the
    # convention used by the other create tests.
    result = e2e.run(
        "mngr create my-task --provider modal --no-start-on-boot --type command --no-connect --no-ensure-clean -- sleep 100099",
        comment="by default, agents are started when a host is booted; this can be disabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the flag actually took effect: the agent must be persisted with
    # start_on_boot disabled (exit code 0 alone would not distinguish this from
    # a normal create). Scope the listing to the modal provider (matching the
    # other create-modal tests): the agent under test lives on modal, and
    # filtering avoids discovering unconfigured default cloud backends (e.g.
    # aws), whose ProviderUnavailableError would otherwise abort an unfiltered
    # listing under the default --on-error abort.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the agent was created with start-on-boot disabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching_agents = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching_agents) == 1, f"Expected exactly one 'my-task' agent, got: {parsed['agents']}"
    assert matching_agents[0]["start_on_boot"] is False, (
        f"Expected start_on_boot to be False, got: {matching_agents[0].get('start_on_boot')}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_create_modal_pass_host_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also set host-level environment variables (separate from agent env vars):
    mngr create my-task --provider modal --pass-host-env MY_VAR
    # --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)
    """)
    # No default agent type is configured in the isolated test profile, so pass
    # --type command (with a long sleep) to stand in for the tutorial's default
    # agent. This also keeps startup fast: --pass-host-env writes MY_VAR to the
    # host env file regardless of the agent type.
    result = e2e.run(
        "MY_VAR=hello mngr create my-task --provider modal --pass-host-env MY_VAR --type command --no-connect --no-ensure-clean -- sleep 100982",
        comment="you can also set host-level environment variables",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the host env var actually reached the host. exec sources the host
    # env file before running the command, so MY_VAR must resolve to "hello".
    env_result = e2e.run(
        "mngr exec my-task -- 'echo MY_VAR=$MY_VAR'",
        comment="confirm the forwarded host env var is set on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("MY_VAR=hello")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_create_modal_host_env_direct(e2e: E2eSession) -> None:
    # Same tutorial block as test_create_modal_pass_host_env, but exercising the
    # direct ``--host-env KEY=VALUE`` form the block's trailing comment documents
    # ("if you really need to you can use --host-env to specify host env vars
    # directly"). Unlike --pass-host-env, this does not read the value from the
    # surrounding shell -- it is supplied inline -- so it goes through a distinct
    # resolution path (resolve_env_vars' explicit_env_var_strings branch).
    e2e.write_tutorial_block("""
    # you can also set host-level environment variables (separate from agent env vars):
    mngr create my-task --provider modal --pass-host-env MY_VAR
    # --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)
    """)
    # No default agent type is configured in the isolated test profile, so pass
    # --type command (with a long sleep) to stand in for the tutorial's default
    # agent. The host env var is supplied inline via --host-env (not forwarded
    # from the shell), so MY_VAR is intentionally absent from the command's env.
    result = e2e.run(
        "mngr create my-task --provider modal --host-env MY_VAR=direct-value --type command --no-connect --no-ensure-clean -- sleep 100983",
        comment="if you really need to you can use --host-env to specify host env vars directly",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the directly-specified host env var actually reached the host. exec
    # sources the host env file before running the command, so MY_VAR must
    # resolve to the inline value -- not anything inherited from the shell.
    env_result = e2e.run(
        "mngr exec my-task -- 'echo MY_VAR=$MY_VAR'",
        comment="confirm the directly-specified host env var is set on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("MY_VAR=direct-value")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_create_modal_reuse(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mngr create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.
    """)
    # The tutorial relies on a configured default agent type; the isolated e2e
    # environment has none, so stand in with `--type command -- sleep ...` (the
    # same lightweight, auth-free agent type the other create e2e tests use).
    create_command = (
        "mngr create sisyphus --reuse --provider modal --no-connect --no-ensure-clean --type command -- sleep 100982"
    )

    # First invocation: the agent does not exist yet, so --reuse falls through
    # to creating it (this is also the call that bootstraps the fresh per-user
    # Modal environment).
    first = e2e.run(
        create_command,
        comment="another handy trick is to make the create command idempotent (first run creates)",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(first).to_succeed()
    # The first run must genuinely *create* the agent (it does not exist yet), so
    # it reports starting a fresh agent and must NOT report reusing one. This pins
    # the observable signal of the "create" branch of --reuse rather than only
    # inferring it later from agent-id equality.
    expect(first.stdout + first.stderr).to_match(r"(?i)starting agent sisyphus")
    expect((first.stdout + first.stderr).lower()).not_to_contain("reusing existing agent")

    after_first = e2e.run(
        "mngr list --provider modal --format json",
        comment="record the agent and host after the first create",
    )
    expect(after_first).to_succeed()
    agents_after_first = [a for a in json.loads(after_first.stdout)["agents"] if a["name"] == "sisyphus"]
    assert len(agents_after_first) == 1, (
        f"Expected exactly one 'sisyphus' agent after first create, got: {agents_after_first}"
    )
    first_agent = agents_after_first[0]
    assert first_agent["host"]["provider_name"] == "modal", first_agent["host"]

    # Second invocation with --reuse: the agent already exists, so it is reused
    # (and started) rather than duplicated. This is the idempotency the tutorial
    # describes -- running create again is safe.
    second = e2e.run(
        create_command,
        comment="if that agent already exists, it will be reused instead of creating a new one (second run reuses)",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(second).to_succeed()
    # The defining behavior of --reuse on the second run is that it reuses the
    # existing agent instead of creating a new one. Assert on the observable
    # signal create emits when it takes the reuse branch ("Reusing existing agent:
    # sisyphus"), which is the direct, human-visible proof that reuse happened --
    # not just that the command exited 0 or that an agent with the same id exists.
    expect(second.stdout + second.stderr).to_match(r"(?i)reusing existing agent")
    expect(second.stdout + second.stderr).to_contain("sisyphus")

    after_second = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the second create reused the existing agent rather than duplicating it",
    )
    expect(after_second).to_succeed()
    agents_after_second = [a for a in json.loads(after_second.stdout)["agents"] if a["name"] == "sisyphus"]
    # Still exactly one agent: --reuse did not create a duplicate.
    assert len(agents_after_second) == 1, (
        f"Expected exactly one 'sisyphus' agent after reuse, got: {agents_after_second}"
    )
    second_agent = agents_after_second[0]
    # The reused agent is the same agent on the same host (not a fresh one).
    assert second_agent["id"] == first_agent["id"], (
        f"Expected reuse to keep the same agent id: first={first_agent['id']} second={second_agent['id']}"
    )
    assert second_agent["host"]["id"] == first_agent["host"]["id"], (
        f"Expected reuse to keep the same host: first={first_agent['host']['id']} second={second_agent['host']['id']}"
    )
    # The tutorial promises the existing agent is reused "(and started)", so the
    # reused agent's host must be running afterwards (not left stopped).
    assert second_agent["host"]["state"] == "RUNNING", (
        f"Expected the reused agent's host to be RUNNING, got: {second_agent['host']['state']!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_basic_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # basic Modal agent (also covered in the CREATING AGENTS REMOTELY section above)
        mngr create my-task --provider modal
    """)
    # The tutorial relies on a configured default agent type (`[commands.create]
    # type`), which the isolated e2e environment does not set. Like the rest of
    # the suite's "default agent" blocks, substitute the built-in `command` type
    # with a long-lived `sleep` so the create exercises full Modal provisioning
    # without needing claude/an API key.
    result = e2e.run(
        "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100205",
        comment="basic Modal agent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the agent actually landed on a running Modal host, rather than only
    # checking the create command's exit code. Scope the listing to --provider
    # modal: a bare `mngr list` fans out to every installed provider plugin and
    # aborts (default --on-error abort) if any unconfigured one (e.g. an installed
    # but credential-less AWS plugin) raises during discovery -- noise unrelated to
    # what this test exercises. Filtering to modal sidesteps that, and we still
    # parse the JSON and tie the my-task agent to its host to confirm it is RUNNING
    # on a real Modal host.
    list_result = e2e.run(
        "mngr list --provider modal --format json", comment="verify the agent is running on Modal"
    )
    expect(list_result).to_succeed()
    agents_by_name = {a["name"]: a for a in json.loads(list_result.stdout)["agents"]}
    assert "my-task" in agents_by_name, f"my-task not found in agents: {list(agents_by_name)}"
    my_task = agents_by_name["my-task"]
    assert my_task["host"]["provider_name"] == "modal", (
        f"Expected my-task on the modal provider, got: {my_task['host']['provider_name']!r}"
    )
    # The host must actually be up (not just recorded), confirming a real Modal
    # host was provisioned by the basic create.
    assert my_task["host"]["state"] == "RUNNING", (
        f"Expected the modal host to be RUNNING, got: {my_task['host']['state']!r}"
    )
    # The create's own arguments must have taken effect, not just that *some*
    # agent was provisioned: the substituted `--type command -- sleep 100205`
    # should be reflected in the persisted agent record.
    assert my_task["type"] == "command", f"Expected a command-type agent, got: {my_task['type']!r}"
    assert "sleep 100205" in my_task["command"], f"Expected the sleep command, got: {my_task['command']!r}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_cpu_memory_gpu(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # specify CPU, memory, and GPU resources
        mngr create my-task --provider modal -b cpu=4 -b memory=16 -b gpu=A10G
    """)
    # GPU=A10G may not be available in the test modal env; drop the gpu arg
    # so the test exercises the cpu+memory build-args without paying for GPU
    # capacity. Keeps the write_tutorial_block intact.
    result = e2e.run(
        "mngr create my-task --provider modal -b cpu=4 -b memory=16 --no-connect --no-ensure-clean",
        comment="specify CPU and memory resources (gpu omitted to avoid quota issues)",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the build args actually took effect rather than only checking the
    # exit code: the new Modal host must be discoverable and report the
    # requested resources. Modal does not expose cpu/memory limits inside the
    # container (nproc and the cgroup memory limit reflect the underlying
    # machine, not the request), so we assert on the resources mngr recorded for
    # the host. cpu.count renders as an int (4); memory_gb as a float (16.0).
    listing = e2e.run(
        "mngr list --provider modal --include 'name == \"my-task\"' "
        "--format 'RES:{name}|{host.resource.cpu.count}|{host.resource.memory_gb}'",
        comment="confirm the host was created with the requested cpu and memory",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(listing).to_succeed()
    expect(listing.stdout).to_contain("RES:my-task|4|16.0")


# No @pytest.mark.rsync here: this create is rejected during build-arg parsing,
# before the git-transfer (rsync) stage runs, so the rsync resource guard would
# fire. The modal mark still holds -- provider initialization invokes the Modal
# CLI (environment_create) before create_host parses the build args.
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_create_modal_invalid_resource_build_arg(e2e: E2eSession) -> None:
    # Same tutorial block as test_create_modal_cpu_memory_gpu, exercised on the
    # unhappy path: the block teaches that CPU/memory/GPU are specified via `-b`
    # build args, so a malformed resource value (here a non-numeric cpu) must be
    # rejected with a clear error rather than silently ignored or producing a
    # confusing downstream failure. This fails during build-arg parsing, before
    # any host is provisioned, so it is cheap relative to the happy-path test.
    e2e.write_tutorial_block("""
        # specify CPU, memory, and GPU resources
        mngr create my-task --provider modal -b cpu=4 -b memory=16 -b gpu=A10G
    """)
    result = e2e.run(
        "mngr create my-task --provider modal -b cpu=not-a-number -b memory=16 --no-connect --no-ensure-clean",
        comment="a malformed resource build arg must be rejected with a clear error",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_fail()
    # The error must name the build-arg parsing failure and the offending arg, so
    # the user can tell which `-b` value was wrong (not just that something failed).
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)invalid build argument")
    expect(combined).to_contain("cpu")

    # The failed create must not leave a phantom agent behind: parsing failed
    # before provisioning, so `my-task` should not appear in the listing at all.
    # Scope to --provider modal (as the happy-path test does) so the listing does
    # not fan out to other providers (e.g. aws) that are unconfigured in the e2e
    # environment and would otherwise make `mngr list` exit non-zero.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the rejected create left no agent behind",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agent_names = [agent["name"] for agent in json.loads(list_result.stdout)["agents"]]
    assert "my-task" not in agent_names, f"Expected no 'my-task' agent after a rejected create, got: {agent_names}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_custom_image_base(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use a custom Docker image as the base
        mngr create my-task --provider modal -b image=python:3.12
    """)
    create_result = e2e.run(
        "mngr create my-task --provider modal -b image=python:3.12 --no-connect --no-ensure-clean",
        comment="use a custom Docker image as the base",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(create_result).to_succeed()
    # Confirm the host genuinely booted from the *custom* base image rather than
    # silently falling back to mngr's default. mngr bakes its required host
    # packages (openssh-server, tmux, rsync, ...) into the default debian_slim
    # base, but a user-supplied registry image does not have them, so create
    # installs them at runtime and warns about it. That warning appears if and
    # only if a custom base image is in use. This is a stronger check than the
    # Python version alone: mngr's default base resolves its Python version to
    # the local interpreter, which is also 3.12 in this test environment, so a
    # version check by itself would pass even if the `-b image=` flag were ignored.
    expect(create_result.stdout + create_result.stderr).to_match(r"(?i)not pre-installed in the base image")
    # Verify the custom image was actually used as the base (not just that the
    # command exited 0): the python:3.12 image ships Python 3.12 as `python`, so
    # exec'ing into the host proves the requested base image is what booted.
    version_result = e2e.run(
        'mngr exec my-task "python --version"',
        comment="confirm the host runs the custom base image's Python 3.12",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(version_result).to_succeed()
    expect(version_result.stdout + version_result.stderr).to_contain("Python 3.12")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_custom_dockerfile_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use a custom Dockerfile
        mngr create my-task --provider modal -b file=./Dockerfile.agent
    """)
    # Write a Dockerfile that bakes a distinctive marker file into the image.
    # Reading it back from the running host (below) proves the host was actually
    # built from *this* Dockerfile rather than the default Modal base image.
    expect(
        e2e.run(
            "printf 'FROM python:3.12\\nRUN echo dockerfile-only-marker > /opt/dockerfile-marker.txt\\n' > Dockerfile.agent",
            comment="write a Dockerfile.agent with a distinctive marker",
        )
    ).to_succeed()
    # The tutorial block relies on the user's configured default agent type;
    # the isolated test profile has none, so pin `--type command -- sleep` (a
    # lightweight agent that needs no install) to exercise the custom-Dockerfile
    # build without provisioning a real coding agent. Mirrors
    # test_create_modal_idle_mode_run.
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b file=./Dockerfile.agent --type command --no-connect --no-ensure-clean -- sleep 100437",
            comment="use a custom Dockerfile",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()
    # Verify the host really runs the custom image by reading back the marker
    # baked into the Dockerfile. This fails if the build silently fell back to
    # the default base image.
    exec_result = e2e.run(
        "mngr exec my-task 'cat /opt/dockerfile-marker.txt'",
        comment="confirm the custom Dockerfile image is in use",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("dockerfile-only-marker")


# No @pytest.mark.rsync here: the create fails during the image build, before the
# git/rsync transfer step ever runs, so the rsync resource guard would fire.
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_create_modal_custom_dockerfile_missing(e2e: E2eSession) -> None:
    # Unhappy-path companion to test_create_modal_custom_dockerfile_only, sharing
    # the same tutorial block: when `-b file=<path>` points at a Dockerfile that
    # does not exist, the create must fail (rather than silently falling back to
    # the default base image) and surface the offending path. This guards the
    # error-handling behavior of the same `--file` build arg the tutorial shows.
    e2e.write_tutorial_block("""
        # use a custom Dockerfile
        mngr create my-task --provider modal -b file=./Dockerfile.agent
    """)
    # Deliberately do NOT create ./Dockerfile.agent, so the referenced Dockerfile
    # is missing. `--type command -- sleep` matches the happy-path test (no claude
    # install / API key needed); the create should error out during the image
    # build, well before any agent would run.
    result = e2e.run(
        "mngr create my-task --provider modal -b file=./Dockerfile.agent --type command --no-connect --no-ensure-clean -- sleep 100438",
        comment="custom Dockerfile path that does not exist should fail",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_fail()
    # The error must name the missing Dockerfile so the user can tell what went
    # wrong, and it must be a clean message -- not a raw Python traceback.
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)dockerfile not found")
    expect(combined).to_contain("Dockerfile.agent")
    assert "Traceback (most recent call last)" not in combined, (
        f"Expected a clean error message, not a Python traceback, got:\n{combined}"
    )

    # The failed create must not leave a phantom agent behind in the listing.
    # Scope to --provider modal: a bare `mngr list` would fan out to every
    # configured provider (including aws/vultr, which are unconfigured in the
    # test env and make the command exit non-zero), whereas this create only ever
    # targeted modal.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm no agent was created from the failed custom-Dockerfile create",
    )
    expect(list_result).to_succeed()
    agent_names = [agent["name"] for agent in json.loads(list_result.stdout)["agents"]]
    assert "my-task" not in agent_names, f"Expected no 'my-task' agent after a failed create, got: {agent_names}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_volume_simple(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # mount a persistent volume for data that survives host destruction
        mngr create my-task --provider modal -b volume=my-data:/data
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b volume=my-data:/data --no-connect --no-ensure-clean",
            comment="mount a persistent volume",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the volume is actually mounted and usable at the requested path,
    # not just that the create command exited 0: write a sentinel into /data and
    # read it back (proves it is a writable filesystem), and confirm /data
    # resolves somewhere other than itself (proves something is mounted there
    # rather than it being a plain empty directory). Avoid asserting on Modal's
    # internal volume mount path so the test stays robust to that detail.
    probe = e2e.run(
        "mngr exec my-task 'set -e;"
        ' marker="mngr-vol-probe-$$";'
        ' echo "$marker" > /data/probe.txt;'
        ' test "$(cat /data/probe.txt)" = "$marker" && echo PROBE_OK;'
        ' resolved="$(readlink -f /data)";'
        ' [ "$resolved" != "/data" ] && echo IS_MOUNT\'',
        comment="verify the persistent volume is mounted and writable at /data",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(probe).to_succeed()
    expect(probe.stdout).to_contain("PROBE_OK")
    expect(probe.stdout).to_contain("IS_MOUNT")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
# Two host creations plus a destroy and two exec round-trips, so this needs more
# headroom than the single-create volume tests.
@pytest.mark.timeout(600)
def test_create_modal_volume_persists_across_destroy(e2e: E2eSession) -> None:
    # Shares the tutorial block with test_create_modal_volume_simple, but
    # exercises the property the comment actually claims -- that the data
    # "survives host destruction" -- rather than only that the mount is writable.
    e2e.write_tutorial_block("""
        # mount a persistent volume for data that survives host destruction
        mngr create my-task --provider modal -b volume=my-data:/data
    """)
    # Create the first agent on a fresh host and write a sentinel into the volume.
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b volume=my-data:/data --no-connect --no-ensure-clean",
            comment="mount a persistent volume and create the first host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr exec my-task 'echo mngr-volume-survives-destroy > /data/persisted.txt'",
            comment="write a sentinel into the persistent volume",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Destroy the agent and its host. A Modal volume is a named resource that
    # lives independently of the sandbox, so destroying the host must not take
    # the volume (or its contents) with it.
    expect(
        e2e.run(
            "mngr destroy my-task --force",
            comment="destroy the first host (the volume should outlive it)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Create a *second*, independent agent on a brand-new host that mounts the
    # same named volume. This is the cross-host sharing / persistence guarantee.
    expect(
        e2e.run(
            "mngr create my-task-redux --provider modal -b volume=my-data:/data --no-connect --no-ensure-clean",
            comment="create a new host mounting the same persistent volume",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # The sentinel written by the (now destroyed) first host must still be
    # readable from the new host, proving the volume's data survived destruction.
    read_back = e2e.run(
        "mngr exec my-task-redux 'cat /data/persisted.txt'",
        comment="confirm the data written before destruction is still present",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(read_back).to_succeed()
    expect(read_back.stdout).to_contain("mngr-volume-survives-destroy")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_idle_timeout_120(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set an idle timeout to avoid runaway costs
        mngr create my-task --provider modal --idle-timeout 120
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-timeout 120 --no-connect --no-ensure-clean",
            comment="set an idle timeout to avoid runaway costs",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the timeout actually took effect on the host, not just that the
    # command exited 0: the agent should report the requested 120 seconds, which
    # is distinct from Modal's 800-second provider default, so this would fail if
    # the flag were silently ignored.
    listing = e2e.run(
        "mngr list --provider modal --format '{name}={idle_timeout_seconds}'",
        comment="confirm the requested idle timeout was applied to the host",
    )
    expect(listing).to_succeed()
    # Match on the exact rendered line rather than a substring: a plain
    # `to_contain("my-task=120")` would also pass for a stray 1200 or 1209, so
    # assert there is exactly one my-task row and that it reports precisely 120.
    agent_lines = [line.strip() for line in listing.stdout.splitlines() if line.strip().startswith("my-task=")]
    assert agent_lines == ["my-task=120"], (
        f"Expected exactly one 'my-task=120' line, got: {agent_lines}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_create_checkpoint(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create a snapshot for checkpointing (useful before risky changes)
        mngr snapshot create my-task --name "checkpoint-1"
    """)
    # The isolated test profile has no default agent type configured, so the
    # setup create (which the tutorial assumes already happened) passes an
    # explicit --type. A `command` agent running `sleep` stays RUNNING, which
    # is the state `mngr snapshot create` ensures before snapshotting.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100991",
            comment="create the modal agent first",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    create_result = e2e.run(
        'mngr snapshot create my-task --name "checkpoint-1"',
        comment="create a snapshot for checkpointing",
    )
    expect(create_result).to_succeed()
    # The create command reports the id of the snapshot it made and the agent(s)
    # whose host was snapshotted (snapshots operate at the host level, resolved
    # from the agent). Capturing that id lets us tie the created snapshot to the
    # one the listing returns below -- proving create -> list consistency rather
    # than just that *some* snapshot named "checkpoint-1" exists. The agent name
    # in the message confirms the my-task -> host resolution actually happened.
    create_output = create_result.stdout + create_result.stderr
    id_match = re.search(r"Created snapshot (\S+) for host", create_output)
    assert id_match is not None, f"Expected create to report the new snapshot id, got:\n{create_output}"
    created_snapshot_id = id_match.group(1)
    assert "agents: my-task" in create_output, (
        f"Expected create to report it snapshotted my-task's host, got:\n{create_output}"
    )

    # Verify the snapshot actually exists, carries the requested name, and is the
    # exact snapshot the create command reported -- rather than trusting the
    # create command's exit code alone.
    list_result = e2e.run(
        "mngr snapshot list my-task --format json",
        comment="verify the checkpoint snapshot was created",
    )
    expect(list_result).to_succeed()
    snapshots = json.loads(list_result.stdout)["snapshots"]
    checkpoint_snapshots = [s for s in snapshots if s.get("name") == "checkpoint-1"]
    assert len(checkpoint_snapshots) == 1, f"Expected exactly one snapshot named 'checkpoint-1', got: {snapshots}"
    assert checkpoint_snapshots[0]["id"] == created_snapshot_id, (
        f"Expected the listed 'checkpoint-1' snapshot id to match the created id "
        f"{created_snapshot_id!r}, got: {checkpoint_snapshots[0]['id']!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_list_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all Modal agents
        mngr list --provider modal
    """)
    # Create a Modal agent first so the listing has a real agent to discover.
    # In a fresh environment the modal provider deliberately short-circuits as
    # empty (it will not bootstrap a Modal environment for read-only commands
    # like `mngr list`), so without an existing agent the command would not
    # exercise Modal at all. Creating one also lets us verify that the listing
    # actually surfaces the agent, not just that the command exits 0. A
    # `command`-type agent is used (with an explicit body) so the create does
    # not depend on a default agent type being configured in the test profile.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100994",
            comment="create a Modal agent to list",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    result = e2e.run("mngr list --provider modal", comment="list all Modal agents", timeout=_REMOTE_TIMEOUT)
    expect(result).to_succeed()
    # The created agent must appear in the Modal listing...
    expect(result.stdout).to_contain("my-task")
    # ...and on a row that attributes it to the modal provider, confirming the
    # --provider modal filter actually surfaced a modal-hosted agent (not just
    # that some listing happened to print the name). The human-readable table
    # places the provider in the same row as the agent name.
    matching_rows = [line for line in result.stdout.splitlines() if "my-task" in line]
    assert any("modal" in row for row in matching_rows), (
        f"Expected the 'my-task' row to show the modal provider, got rows: {matching_rows}"
    )

    # Prove the --provider flag actually discriminates rather than printing
    # everything: it is a fan-out control that selects which providers to query.
    # Querying a *different* provider (local) must therefore NOT surface the
    # modal-hosted agent. This is a cheap, local-only listing (no extra Modal
    # provisioning) that confirms the modal-hosted agent is excluded when a
    # non-modal provider is requested.
    local_only = e2e.run(
        "mngr list --provider local",
        comment="confirm --provider filters: a local-only listing excludes the modal agent",
    )
    expect(local_only).to_succeed()
    expect(local_only.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
# Generous overall budget: this test does a remote modal create, a list, a
# remote modal destroy (host teardown), and a final list -- each a network
# round-trip, and destroy in particular is slow in the Modal-in-Modal offload
# environment.
@pytest.mark.timeout(360)
def test_destroy_all_modal_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all Modal agents (be careful!)  Useful for cleaning up while prototyping
        mngr list --include 'host.provider == "modal"' --ids | mngr destroy -f -
    """)
    # Create a real Modal agent so the destroy-all command has something to act
    # on -- this verifies the filter+stdin pipeline actually destroys agents
    # rather than just succeeding against an empty environment.
    expect(
        e2e.run(
            "mngr create doomed-agent --provider modal --type command --no-connect --no-ensure-clean -- sleep 100605",
            comment="create a Modal agent to be cleaned up",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Confirm the agent is present among Modal agents before destroying it.
    # The verification steps scope the listing to `--provider modal` (rather than
    # the tutorial's `--include 'host.provider == "modal"'` post-filter): a bare
    # `mngr list` queries *every* enabled provider and exits 1 if any is
    # unreachable (e.g. an unconfigured aws backend, or a docker daemon that is
    # not running in a Modal-only test sandbox). `--provider modal` only queries
    # the modal backend, so these standalone assertions stay robust regardless of
    # which other providers happen to be available in the test environment. The
    # destroy pipeline below still runs the tutorial command verbatim.
    before = e2e.run(
        "mngr list --provider modal",
        comment="list Modal agents before destroying",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(before).to_succeed()
    expect(before.stdout).to_contain("doomed-agent")
    # Destroy all Modal agents via the filter+stdin pipeline from the tutorial.
    # `mngr list --include ... --ids` prints the matching modal ids to stdout
    # (any unreachable-provider error goes to stderr), so the pipe still feeds
    # the modal ids to `mngr destroy`, whose exit code is what the shell returns.
    # The pipeline tears down a remote Modal host, so it needs the remote timeout
    # rather than the short default.
    destroy_result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --ids | mngr destroy -f -",
        comment="destroy all Modal agents via filter+stdin",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(destroy_result).to_succeed()
    # The filter+stdin pipeline must have actually fed the modal agent's id to
    # `mngr destroy` and destroyed it -- assert on the destroy output so the test
    # does not pass vacuously if `mngr list --ids` ever produced no ids (which
    # would leave `mngr destroy -f -` reading empty stdin and still exiting 0).
    expect(destroy_result.stdout).to_contain("doomed-agent")
    expect(destroy_result.stdout).to_match(r"(?i)destroyed 1 agent")
    # The agent should no longer appear among active Modal agents.
    after = e2e.run(
        "mngr list --provider modal",
        comment="list Modal agents after destroying",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(after).to_succeed()
    expect(after.stdout).not_to_contain("doomed-agent")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_create_modal_idle_timeout_60(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set an idle timeout (in seconds) -- the agent's host will stop after this much inactivity
        mngr create my-task --provider modal --idle-timeout 60
    """)
    # This test does two remote round-trips (create + list) and the create's
    # final "initial snapshot" step is slow and variable in the Modal-in-Modal
    # offload environment, so give create a more generous timeout than the
    # default _REMOTE_TIMEOUT to avoid flaky timeouts.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-timeout 60 --no-connect --no-ensure-clean",
            comment="set an idle timeout (in seconds)",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()

    # Verify the idle timeout was actually applied to the host, not just that
    # the create command exited 0: the created agent should report a 60s
    # idle_timeout_seconds (the value enforced by the host's activity watcher).
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="the agent's host will stop after this much inactivity",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds=60, got: {matching[0]['idle_timeout_seconds']}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_idle_mode_ssh_timeout_300(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # control what counts as "activity" with --idle-mode:
        #   "agent" (default) -- idle when the agent process is idle
        #   "ssh" -- idle when no SSH sessions are connected
        #   "run" -- idle when the main process exits (useful for non-agent commands)
        #   ...
        # see the idle_detection.md file for more details on idle detection strategies
        mngr create my-task --provider modal --idle-mode ssh --idle-timeout 300
    """)
    # The tutorial block relies on the user's configured default agent type;
    # the isolated e2e env has none, so we pass an explicit lightweight
    # `--type command -- sleep <N>` (matching the convention used by the other
    # tutorial create tests) to exercise --idle-mode/--idle-timeout end to end.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-mode ssh --idle-timeout 300 --no-connect --no-ensure-clean"
            " --type command -- sleep 100981",
            comment="control what counts as activity with --idle-mode",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the requested idle configuration was actually applied to the host,
    # not just that create exited 0: list the agent and assert idle_mode 'ssh'
    # and idle_timeout_seconds 300. The non-terminating sleep keeps the host
    # online so these fields are surfaced (they are only reported while online),
    # and the 300s timeout leaves ample margin before any idle shutdown.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="idle when no SSH sessions are connected",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    agent = matching[0]
    assert agent["idle_mode"] is not None and agent["idle_mode"].lower() == "ssh", (
        f"Expected idle_mode 'ssh', got: {agent['idle_mode']}"
    )
    assert agent["idle_timeout_seconds"] == 300, (
        f"Expected idle_timeout_seconds 300, got: {agent['idle_timeout_seconds']}"
    )


# No @pytest.mark.modal here: an invalid --idle-mode value is rejected by click's
# option parsing (exit code 2) before the command body runs, so no Modal API call
# is ever made and the modal resource guard would otherwise fire.
@pytest.mark.release
@pytest.mark.timeout(120)
def test_create_modal_idle_mode_invalid(e2e: E2eSession) -> None:
    """Unhappy path for the --idle-mode tutorial block: an unsupported mode is rejected."""
    e2e.write_tutorial_block("""
        # control what counts as "activity" with --idle-mode:
        #   "agent" (default) -- idle when the agent process is idle
        #   "ssh" -- idle when no SSH sessions are connected
        #   "run" -- idle when the main process exits (useful for non-agent commands)
        #   ...
        # see the idle_detection.md file for more details on idle detection strategies
        mngr create my-task --provider modal --idle-mode ssh --idle-timeout 300
    """)
    # The tutorial only documents a fixed set of idle modes; passing anything else
    # must be rejected up front (a typo'd mode should not silently create a host).
    result = e2e.run(
        "mngr create my-task --provider modal --idle-mode bogus --idle-timeout 300"
        " --no-connect --no-ensure-clean --type command -- sleep 100982",
        comment="an unsupported --idle-mode value is rejected before any host is created",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    # The error must name the offending option and reject the bad value...
    expect(combined).to_contain("--idle-mode")
    expect(combined).to_contain("bogus")
    # ...while pointing the user at the valid modes the tutorial documents.
    expect(combined).to_contain("ssh")
    expect(combined).to_contain("agent")
    expect(combined).to_contain("run")
    # The bad mode must not have leaked into a created agent.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm no agent was created for the rejected idle mode",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    assert not [agent for agent in agents if agent["name"] == "my-task"], (
        f"Expected no 'my-task' agent after a rejected --idle-mode, got: {agents}"
    )


# Flaky: the remote Modal create path occasionally drops the SSH session
# mid-create (observed as an error in the `_ensure_shared_shell_libs` thread,
# "SSH error (No existing session)") or hits a transient Modal permission error
# while listing sandboxes during failure cleanup. Both are infrastructure
# transients in the create/SSH layer, not in this test's logic, so let offload
# retry rather than fail the run.
@pytest.mark.flaky
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_idle_mode_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # for long-running scripts, "run" mode stops the host when the script finishes
        mngr create my-task --provider modal --type command --idle-mode run --idle-timeout 60 -- python long_job.py
    """)
    # Substitute `sleep` for the missing long_job.py; the test wants to
    # verify --idle-mode run is accepted with --type command. The long sleep
    # never exits, so under "run" idle mode the host stays up and `mngr list`
    # below observes it online.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --idle-mode run --idle-timeout 60 --no-connect --no-ensure-clean -- sleep 100980",
            comment="run mode stops the host when the script finishes",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the concrete effect: a command-type agent really exists, runs our
    # command, and carries the requested idle configuration (run mode, 60s).
    # Scope the listing to --provider modal: a bare `mngr list` would discover
    # every enabled backend, and any backend that is enabled-but-unconfigured in
    # the environment (e.g. aws without credentials) hard-fails the whole command
    # with a ProviderDiscoveryError. We only care about the modal agent here, so
    # filtering keeps the verification robust to unrelated providers.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the command agent was created with run idle mode",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    agent = matching[0]
    assert agent["type"] == "command", f"Expected a command-type agent, got: {agent['type']}"
    assert "sleep 100980" in agent["command"], f"Expected the sleep command, got: {agent['command']}"
    # idle_mode / idle_timeout_seconds are only surfaced while the host is
    # online; the non-terminating sleep keeps it up under run mode, so assert
    # the requested configuration was actually applied.
    assert agent["idle_mode"] is not None and agent["idle_mode"].lower() == "run", (
        f"Expected idle_mode 'run', got: {agent['idle_mode']}"
    )
    assert agent["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds 60, got: {agent['idle_timeout_seconds']}"
    )


# Flaky: the `mngr list` issued right after `mngr stop agent-1` occasionally comes
# back empty ("No agents found") because Modal sandbox discovery is eventually
# consistent -- the just-stopped agent's host can momentarily drop out of the
# listing even though agent-2 is still running on it. This is an infrastructure
# transient in Modal discovery, not in this test's logic, so let offload retry
# rather than fail the run.
@pytest.mark.flaky
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_create_modal_multiple_agents_one_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create a first agent on a named host
        mngr create agent-1@shared-host.modal --provider modal --new-host
        # create additional agents on the same host using the address syntax
        mngr create agent-2@shared-host.modal
        # all agents on the same host share the filesystem and network,
        # so they can collaborate on the same codebase
        # list agents to see which ones share a host
        mngr list --fields "name,state,host.name"
        # stop one agent without affecting the others
        mngr stop agent-1
        # the host stays running as long as at least one agent is active.
    """)
    # The tutorial uses the configured default agent type; the test substitutes
    # the lightweight built-in `command` type running a long sleep so the agents
    # stay active (keeping the shared host up) without a real agent startup.
    expect(
        e2e.run(
            "mngr create agent-1@shared-host.modal --provider modal --new-host --type command --no-connect --no-ensure-clean -- sleep 100990",
            comment="create first agent on a named host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create agent-2@shared-host.modal --type command --no-connect --no-ensure-clean -- sleep 100991",
            comment="create additional agents on the same host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    shared_host_list = e2e.run(
        'mngr list --fields "name,state,host.name"',
        comment="list agents to see which share a host",
    )
    expect(shared_host_list).to_succeed()
    # Both agents must be running on the same named host (the whole point of the
    # address syntax): each agent row should reference 'shared-host'.
    expect(shared_host_list.stdout).to_match(r"agent-1\s+\S+\s+shared-host")
    expect(shared_host_list.stdout).to_match(r"agent-2\s+\S+\s+shared-host")

    # Stopping one agent must not affect the other; the host stays running as
    # long as at least one agent is active.
    stop_result = e2e.run("mngr stop agent-1", comment="stop one agent without affecting others")
    expect(stop_result).to_succeed()
    expect(stop_result.stdout).to_contain("Stopped agent: agent-1")

    # agent-2 (and therefore the shared host) is still up after stopping agent-1.
    after_stop_list = e2e.run(
        'mngr list --fields "name,state,host.name"',
        comment="confirm the other agent and its host are unaffected",
    )
    expect(after_stop_list).to_succeed()
    # agent-1 actually transitioned to STOPPED on the shared host (the stop took
    # effect on state, not just printed a message)...
    expect(after_stop_list.stdout).to_match(r"agent-1\s+STOPPED\s+shared-host")
    # ...while agent-2 stays active (not STOPPED) on the same host, which is what
    # keeps the shared host running.
    expect(after_stop_list.stdout).to_match(r"agent-2\s+(?!STOPPED)\S+\s+shared-host")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_upload_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # upload a file to the agent's host during creation
        mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config
    """)
    # The tutorial relies on a configured default agent type; the test pins
    # --type command (a lightweight sleep agent) so it doesn't pay claude
    # startup time, matching the substitution used by the other modal tests.
    # Give the local ~/.ssh/config known content so we can confirm the upload
    # actually landed on the remote host (not just that create exited 0).
    expect(
        e2e.run(
            "mkdir -p ~/.ssh && printf 'Host example\\n    HostName 203.0.113.7\\n' > ~/.ssh/config",
            comment="ensure ssh config exists",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config"
            " --type command --no-connect --no-ensure-clean -- sleep 100631",
            comment="upload a file to the agent's host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the file was actually uploaded to the requested target path on the
    # remote host, with its contents intact.
    upload_check = e2e.run(
        "mngr exec my-task 'cat /root/.ssh/config'",
        comment="confirm the uploaded file landed on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(upload_check).to_succeed()
    expect(upload_check.stdout).to_contain("HostName 203.0.113.7")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_pip_install(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a setup command during host provisioning
        mngr create my-task --provider modal --extra-provision-command "pip install numpy pandas"
    """)
    # `pip install numpy pandas` from the tutorial pulls large packages, which is
    # slow at provision time. Substitute the smallest possible stand-in that still
    # exercises the tutorial's headline behavior -- installing a Python package via
    # a provision command: `six` is a tiny, dependency-free, pure-Python package.
    # Importing it back via `mngr exec` (below) proves the --extra-provision-command
    # actually ran pip during provisioning, not just that the create command exited
    # 0. Use --type command -- sleep (the convention shared with the other create
    # tests) so the agent stays up for the exec round-trip without needing claude
    # installed/authenticated.
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            ' --extra-provision-command "pip install six"'
            " --type command --no-connect --no-ensure-clean -- sleep 100156",
            comment="run a setup command during host provisioning (install a small package)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the provision command actually installed the package: the module must
    # be importable on the host. Exit code 0 of the create alone would not prove the
    # pip install ran. Printing the version records concrete evidence in the
    # transcript that the package is genuinely installed (a stand-in for verifying
    # numpy/pandas would import in the real tutorial command).
    import_result = e2e.run(
        "mngr exec my-task 'python -c \"import six; print(six.__version__)\"'",
        comment="confirm the pip-installed package is importable on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(import_result).to_succeed()
    expect(import_result.stdout).to_match(r"\d+\.\d+")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_sudo_apt(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command as root during provisioning (if your default user is not root, assumes passwordless sudo for that user)
        mngr create my-task --provider modal --extra-provision-command "sudo apt-get update && apt-get install -y vim"
    """)
    # The tutorial block demonstrates running a provisioning command with root
    # privileges. The Modal default user is already root (ssh_user="root") and the
    # default image ships no `sudo`, so we substitute the slow, network-heavy
    # apt-get command with one that records the effective uid to a marker file.
    # This still verifies the core claim of the block -- that extra-provision
    # commands run as root -- without the apt cost. As in the other provision
    # tests, we pass --type command -- sleep to keep the host alive for the
    # exec-based verification below.
    marker_path = "/tmp/mngr_sudo_marker"
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            f' --extra-provision-command "id -u > {marker_path}"'
            " --type command --no-connect --no-ensure-clean -- sleep 100985",
            comment="run a command as root during provisioning (substituted to record the effective uid)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Exit code 0 alone would not prove the provision command actually ran with
    # root privileges -- read back the marker to confirm it executed as uid 0.
    marker_result = e2e.run(
        f"mngr exec my-task 'cat {marker_path}'",
        comment="confirm the extra provision command ran as root (uid 0) on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(marker_result).to_succeed()
    # `mngr exec` appends its own status line (e.g. "Command succeeded ...") after
    # the command output, so check the first line -- the marker file's contents.
    marker_contents = marker_result.stdout.splitlines()[0].strip()
    assert marker_contents == "0", (
        f"Expected the provision command to run as root (uid 0), got: {marker_result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_append_file(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # append content to a file on the host using a provision command
        mngr create my-task --provider modal --extra-provision-command "echo 'export PATH=/opt/bin:\$PATH' >> /root/.bashrc"
    """)
    # The tutorial block relies on the user's configured default agent type;
    # like every other e2e create test we pass --type command -- sleep so the
    # test is self-contained and keeps the agent alive for the exec-based
    # verification below. Mirror the tutorial's "append to a file" behavior, but
    # target a throwaway path instead of /root/.bashrc so we never mutate the
    # host's shell config.
    #
    # The headline of this block is the append operator (``>>``), as opposed to
    # the sibling pip-install/sudo tests which overwrite with ``>``. To prove the
    # append semantics rather than just that *some* content landed, seed the file
    # with a baseline line via a first (repeatable) --extra-provision-command, then
    # append the tutorial's PATH line with ``>>``. If ``>>`` were behaving like
    # ``>`` (truncate), the baseline line would be gone -- so asserting that the
    # baseline survives *and* precedes the appended line confirms a real append.
    marker_path = "/tmp/mngr_provision_marker"
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            f" --extra-provision-command \"echo provision-baseline-line > {marker_path}\""
            f" --extra-provision-command \"echo 'export PATH=/opt/bin:\\$PATH' >> {marker_path}\""
            " --type command --no-connect --no-ensure-clean -- sleep 100985",
            comment="append to a file on the host (targeting a throwaway path instead of bashrc)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the provision command actually ran on the host and appended the
    # expected line -- exit code 0 alone would not prove the file was written.
    marker_result = e2e.run(
        f"mngr exec my-task 'cat {marker_path}'",
        comment="confirm the extra provision command appended to the file on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(marker_result).to_succeed()
    appended_line = "export PATH=/opt/bin:$PATH"
    assert appended_line in marker_result.stdout, (
        f"Expected the appended line in {marker_path}, got: {marker_result.stdout!r}"
    )
    # The baseline line must still be present (proving ``>>`` appended rather than
    # truncated) and must precede the appended PATH line (proving the append went
    # to the end of the existing content).
    assert "provision-baseline-line" in marker_result.stdout, (
        f"Expected the pre-existing baseline line to survive the append in {marker_path}, "
        f"got: {marker_result.stdout!r}"
    )
    assert marker_result.stdout.index("provision-baseline-line") < marker_result.stdout.index(appended_line), (
        f"Expected the baseline line to precede the appended line (real append, not truncate), "
        f"got: {marker_result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_combined_setup_steps(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # combine multiple setup steps (--extra-provision-command is repeatable and runs in order)
        mngr create my-task --provider modal \
          --upload-file ./requirements.txt:/workspace/requirements.txt \
          --extra-provision-command "apt-get update && apt-get install -y build-essential" \
          --extra-provision-command "pip install -r /workspace/requirements.txt"
    """)
    expect(e2e.run("echo 'requests==2.32.0' > requirements.txt", comment="write requirements.txt")).to_succeed()
    # Substitute the slow apt-get/pip steps with quick provision commands that each
    # append a marker line to a host file. This keeps the test fast while making
    # the core behavior of the tutorial block observable: that --extra-provision-command
    # is repeatable and runs in the order given. Crucially, the second provision
    # command *reads the uploaded file* (`cat /workspace/requirements.txt`), exactly
    # as the tutorial's `pip install -r /workspace/requirements.txt` does. This
    # makes the test faithful to the real dependency the tutorial block relies on:
    # --upload-file must land before the extra-provision-commands run, otherwise a
    # provision step that consumes the uploaded file would fail.
    # The tutorial block omits --type because it assumes onboarding configured a
    # default agent type; the isolated test profile has none, so we pass an
    # explicit type here. We use the lightweight `command` agent (running a long
    # `sleep` to keep the host alive) instead of the default claude agent, which
    # would install Claude Code on the host and is far slower.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command"
            " --upload-file ./requirements.txt:/workspace/requirements.txt"
            ' --extra-provision-command "echo build-step >> /tmp/provision_order.log"'
            ' --extra-provision-command "cat /workspace/requirements.txt >> /tmp/provision_order.log'
            ' && echo provision-step >> /tmp/provision_order.log"'
            " --no-connect --no-ensure-clean -- sleep 600",
            comment="combine upload + repeated extra-provision (substituted for speed)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the upload actually landed on the host with the expected content,
    # rather than only trusting the create command's exit code.
    upload_check = e2e.run(
        'mngr exec my-task "cat /workspace/requirements.txt"',
        comment="verify the uploaded file is present on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(upload_check).to_succeed()
    expect(upload_check.stdout).to_contain("requests==2.32.0")
    # Verify both repeated --extra-provision-command flags actually ran, and ran
    # in the order they were given on the command line. This is the headline
    # behavior the tutorial block demonstrates ("repeatable and runs in order").
    provision_check = e2e.run(
        'mngr exec my-task "cat /tmp/provision_order.log"',
        comment="verify both provision commands ran, in the order given",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(provision_check).to_succeed()
    expect(provision_check.stdout).to_contain("build-step")
    expect(provision_check.stdout).to_contain("provision-step")
    # The second provision command catted the uploaded requirements.txt into the
    # log, so its contents appearing here proves the --upload-file completed
    # *before* the extra-provision-commands ran -- the exact ordering dependency
    # the tutorial relies on when it pairs --upload-file with `pip install -r`.
    expect(provision_check.stdout).to_contain("requests==2.32.0")
    build_index = provision_check.stdout.index("build-step")
    requirements_index = provision_check.stdout.index("requests==2.32.0")
    provision_index = provision_check.stdout.index("provision-step")
    assert build_index < requirements_index < provision_index, (
        "Expected provision steps to run in order with the uploaded requirements.txt read by the "
        f"second step, got: {provision_check.stdout!r}"
    )
