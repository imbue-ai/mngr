"""Tests for Modal agent creation from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block. This makes it
easy to maintain the mapping between tutorial content and test coverage via the
tutorial_matcher script.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
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
@pytest.mark.timeout(240)
def test_create_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also launch your default agent remotely in Modal:
        mngr create my-task --provider modal
        # see more details below in "CREATING AGENTS REMOTELY" for relevant options
    """)
    # The tutorial command launches "your default agent", which assumes a
    # default agent type is configured. The isolated test profile has none, so
    # we make the agent type explicit. As elsewhere in these tests, we use the
    # `command` type running a long `sleep` instead of a real claude agent --
    # this exercises the `--provider modal` path (the point of this block)
    # without paying for a claude install + auth on the remote host.
    result = e2e.run(
        "mngr create my-task --provider modal --no-connect --no-ensure-clean --type command -- sleep 100050",
        comment="you can also launch your default agent remotely in Modal",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent was actually created on a Modal host (not just that the
    # command exited 0): it should appear in `mngr list` with provider=modal.
    list_result = e2e.run(
        "mngr list --format json --provider modal",
        comment="verify the agent is running on a Modal host",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {parsed['agents']}"
    assert matching[0]["host"]["provider_name"] == "modal", matching[0]


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_create_modal_no_connect_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
    mngr create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github"
    # here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
    # and then we also pass in an explicit message for the agent to start working on immediately
    # the message can also be specified as the contents of a file (by using --message-file instead of --message)
    """)
    # The tutorial relies on the user's configured default agent type; the
    # isolated test profile has none, so substitute `--type command -- sleep`
    # (the same substitution test_create_with_message uses for the local
    # variant). This still exercises the full message-on-launch path: mngr
    # boots the Modal host, waits for the agent to signal readiness, then
    # delivers the initial message via the agent's tmux pane.
    #
    # A generous ready timeout covers Modal container boot, which is slow in
    # offload's Modal-in-Modal environment. A command agent needs no Claude
    # install/auth, so the original ~5-8 min budget is unnecessary.
    result = e2e.run(
        "MNGR__AGENT_READY_TIMEOUT=300 mngr create my-task --provider modal --no-connect"
        ' --message "Speed up one of my tests and make a PR on github"'
        " --no-ensure-clean --type command -- sleep 101085",
        comment="you can send an initial message (so you don't have to wait around)",
        timeout=360.0,
    )
    if result.exit_code != 0:
        diagnostics = e2e.collect_remote_diagnostics("my-task")
        raise AssertionError(
            f"Expected command to succeed but got exit code {result.exit_code}\n"
            f"  Command: {result.command}\n"
            f"  Stderr:\n    {result.stderr}\n"
            f"{diagnostics}"
        )
    # This line is logged only after the agent signals readiness and mngr
    # begins delivering the initial message, so its presence confirms the
    # message-on-launch path actually ran (not just that create succeeded).
    expect(result.stderr).to_contain("Sending initial message")
    # Verify the agent really landed on a Modal host (not the local provider).
    list_result = e2e.run(
        "mngr list --include 'host.provider == \"modal\"' --format json",
        comment="verify the agent is running on a Modal host",
    )
    expect(list_result).to_succeed()
    modal_agents = json.loads(list_result.stdout)["agents"]
    assert [agent for agent in modal_agents if agent["name"] == "my-task"], (
        f"Expected 'my-task' on a Modal host, got: {[agent['name'] for agent in modal_agents]}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_edit_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mngr create my-task --provider modal --edit-message
    """)
    # --edit-message launches $EDITOR to compose the message in parallel with
    # host provisioning. In a non-interactive test there is no terminal, so we
    # point EDITOR at `true`: it exits 0 immediately without writing to the temp
    # file, exercising the editor-launch/await flow while the agent is created
    # (the empty result means no message is sent, which is the "closed without
    # typing" path). --type command -- sleep ... stands in for the default agent
    # type, which the isolated test profile does not configure.
    result = e2e.run(
        "VISUAL=true EDITOR=true mngr create my-task --provider modal --type command --edit-message"
        " --no-connect --no-ensure-clean -- sleep 100981",
        comment="you can also edit the message *while the agent is starting up*",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the agent was genuinely created on a Modal host (not just that the
    # command exited 0): it must show up under the modal provider with the name
    # we asked for. The editor closed without writing, so no message was sent --
    # creation itself is what --edit-message must not break.
    listed = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the agent was created on a Modal host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(listed).to_succeed()
    agents = json.loads(listed.stdout)["agents"]
    assert "my-task" in [agent["name"] for agent in agents], (
        f"Expected an agent named 'my-task' on modal, got: {agents}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_rsync(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    # Seed the working tree with data that git transfer alone would NOT carry:
    #   - rsync_marker.txt: an untracked file (the "extra data beyond just the
    #     git data" that --rsync is meant to transfer).
    #   - node_modules/: a *gitignored* directory. It must be gitignored (not
    #     merely untracked) so that mngr's uncommitted-changes transfer skips it
    #     -- that path uses `git status` + `rsync --files-from`, which ignores
    #     rsync excludes. With node_modules gitignored, the only thing that would
    #     copy it is the --rsync full-tree pass, so --rsync-args
    #     "--exclude=node_modules" is what actually keeps it off the remote. This
    #     mirrors the real-world case (node_modules is conventionally gitignored).
    expect(
        e2e.run(
            "echo 'node_modules/' >> .gitignore"
            " && echo rsync-extra-data > rsync_marker.txt"
            " && mkdir -p node_modules && echo junk > node_modules/junk.txt",
            comment="seed untracked extra data and a gitignored node_modules dir to be excluded",
        )
    ).to_succeed()
    result = e2e.run(
        'mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules" --no-connect --no-ensure-clean',
        comment="you can use rsync to transfer extra data as well",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Inspect the agent's work_dir on the remote host (mngr exec defaults to the
    # work_dir). The untracked marker must be present (rsync carried non-git
    # data), and node_modules must be absent (the --exclude rsync arg applied).
    verify = e2e.run(
        'mngr exec my-task "cat rsync_marker.txt;'
        ' (test -d node_modules && echo NODE_MODULES_PRESENT || echo NODE_MODULES_ABSENT)"',
        comment="verify rsync transferred extra data and honored --exclude=node_modules",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(verify).to_succeed()
    expect(verify.stdout).to_contain("rsync-extra-data")
    expect(verify.stdout).to_contain("NODE_MODULES_ABSENT")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_passthrough_agent_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # one of the coolest features of mngr is the ability to create agents on remote hosts just as easily as you can create them locally:
    mngr create my-task --provider modal -- --dangerously-skip-permissions --append-system-prompt "Don't ask me any questions!"
    # that command passes the "--dangerously-skip-permissions" flag to claude because it's safe to do so:
    # agents running remotely are running in a sandboxed environment where they can't really mess anything up on their local machine (or if they do, it doesn't matter)
    # because it's running remotely, you might also want something like that system prompt (to tell it not to get blocked on you)
    """)
    # The tutorial assumes a configured default agent type; the isolated test
    # environment has none, so we pass --type claude explicitly. claude is also
    # the type the passthrough flags below (--dangerously-skip-permissions,
    # --append-system-prompt) are meant for, keeping the test faithful.
    result = e2e.run(
        'mngr create my-task --provider modal --type claude --no-connect --no-ensure-clean -- --dangerously-skip-permissions --append-system-prompt "Don\'t ask me any questions!"',
        comment="one of the coolest features of mngr is the ability to create agents on remote hosts",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the passthrough args actually reached the agent's launch command
    # (the whole point of the `-- ...` syntax). The assembled command is
    # persisted on the host and surfaced as the `command` field of `mngr list`.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the passthrough args were baked into the agent's launch command",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {[a['name'] for a in agents]}"
    launch_command = matching[0]["command"]
    assert "--dangerously-skip-permissions" in launch_command, (
        f"Expected passthrough flag in launch command, got: {launch_command!r}"
    )
    assert "--append-system-prompt" in launch_command, (
        f"Expected passthrough flag in launch command, got: {launch_command!r}"
    )
    # The system-prompt value is shell-quoted in the assembled command (the
    # apostrophe in "Don't" becomes '"'"'), so assert on the contiguous portion
    # that survives quoting rather than the raw string.
    assert "ask me any questions!" in launch_command, (
        f"Expected passthrough system-prompt value in launch command, got: {launch_command!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_idle_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
    # one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
    # mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
    mngr create my-task --provider modal --idle-timeout 60
    # that command shuts down the Modal host (and agent) after 1 minute of inactivity.
    """)
    # The tutorial block omits --type because it assumes a configured default
    # agent type. The test fixture sets no default, so (matching the convention
    # used elsewhere in this file, e.g. test_create_modal_idle_mode_run) we use
    # --type command with a trivial sleep -- this exercises --idle-timeout 60 on
    # Modal without paying for a full Claude agent startup.
    result = e2e.run(
        "mngr create my-task --provider modal --idle-timeout 60 --type command --no-connect --no-ensure-clean -- sleep 100937",
        comment="mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the idle timeout was actually applied to the created agent, not
    # just that the flag was accepted. The agent JSON view exposes
    # idle_timeout_seconds, so we can confirm the 60s timeout took effect.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the idle timeout was applied to the agent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds=60, got: {matching[0].get('idle_timeout_seconds')}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_idle_mode_ssh(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # You can customize what "inactivity" means by using the --idle-mode flag:
    mngr create my-task --provider modal --idle-mode "ssh"
    # that command will only consider agents as "idle" when you are not connected to them
    # see the idle_detection.md file for more details on idle detection and timeouts
    """)
    result = e2e.run(
        'mngr create my-task --provider modal --idle-mode "ssh" --no-connect --no-ensure-clean',
        comment="You can customize what inactivity means by using the --idle-mode flag",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent was actually configured for ssh idle mode -- not just
    # that the flag parsed. The host reports its live activity config via
    # `mngr list`, and ssh mode resolves to exactly the SSH/CREATE/START/BOOT
    # activity sources. Crucially, agent output and user input do NOT count as
    # activity in ssh mode, which is what makes it differ from the default io
    # mode and is the behavior the tutorial is demonstrating.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent is configured for ssh idle mode",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    matching = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got {matching}"
    agent = matching[0]
    assert (agent["idle_mode"] or "").lower() == "ssh", f"expected idle_mode 'ssh', got {agent['idle_mode']!r}"
    activity_sources = {source.lower() for source in (agent["activity_sources"] or ())}
    assert activity_sources == {
        "ssh",
        "create",
        "start",
        "boot",
    }, f"expected ssh-mode activity sources, got {activity_sources}"


# No @pytest.mark.modal: this is a negative test that fails at host resolution
# ("Could not find host") without provider in the address, so it never actually
# invokes Modal. The resource guard flags a superfluous modal mark on passing
# tests, so the mark is intentionally omitted here.
@pytest.mark.release
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    # The tutorial line assumes the user already has a default agent type
    # configured; the isolated test profile does not, so we pass --type command
    # explicitly. The agent type is resolved before host resolution, so without
    # it the command would fail on the missing default type rather than the
    # missing host (which is the behavior this test means to exercise). The host
    # name "my-dev-box" does not exist, so creation fails when resolving it.
    result = e2e.run(
        "mngr create my-task@my-dev-box --type command --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
    )
    expect(result).to_fail()
    # The error should name the host that could not be found.
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)could not find host: my-dev-box")


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
    result = e2e.run(
        "mngr create my-task --provider modal -b cpu=4 -b memory=16 -b image=python:3.12 --no-connect --no-ensure-clean",
        comment="build arguments let you customize that new remote host",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_succeed()
    # Verify the build args actually took effect, not just that the flags parsed.
    # The `image=python:3.12` arg is the most distinctive to check: the stock
    # mngr base image ships a different Python, so a host whose default python3
    # reports 3.12 confirms the custom base image was genuinely used.
    version_result = e2e.run(
        'mngr exec my-task "python3 --version"',
        comment="verify the python:3.12 base image was actually used for the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(version_result).to_succeed()
    expect(version_result.stdout + version_result.stderr).to_contain("3.12")


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
    # Create the Dockerfile and context directory so the build args have real
    # targets. Critically, the Dockerfile COPYs a file *out of the context dir*:
    # this makes a successful build prove that BOTH build args took effect. The
    # COPY instruction is only present because --file pointed at our custom
    # Dockerfile, and the COPY can only resolve `marker.txt` because --context-dir
    # pointed at the directory containing it. If --context-dir were ignored, the
    # COPY source would be missing and `mngr create` below would fail. (The
    # original version used a bare `FROM` with an empty context, so --context-dir
    # was passed but never actually exercised by the build.)
    #
    # We rely on `mngr create` succeeding rather than inspecting the build log or
    # `mngr exec`-ing into the host: the build log omits the COPY step on a Modal
    # image-cache hit, and the Modal environment name is truncated to 64 chars,
    # which can collapse sibling tests' "my-task" agents into one listing -- both
    # make those checks unreliable, while the build's success/failure does not.
    e2e.run(
        "mkdir -p agent-context"
        " && echo 'modal-context-marker' > agent-context/marker.txt"
        " && printf 'FROM python:3.12-slim\\nCOPY marker.txt /opt/marker.txt\\n' > Dockerfile.agent",
        comment="create Dockerfile (COPYing from the context dir) and context",
    )
    result = e2e.run(
        "mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context --no-connect --no-ensure-clean",
        comment="the most important build args for Modal are --file and --context-dir",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_named_host_new_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can name the host using the address syntax:
    mngr create my-task@my-modal-box.modal --new-host
    # (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)
    """)
    # The tutorial assumes the user has configured a default agent type (see the
    # BASIC CREATION block: "agent=your configured default"). The isolated e2e
    # profile sets no default, so we pass an explicit lightweight command agent
    # (`-- sleep N`), matching the other Modal command-agent tests in this file.
    result = e2e.run(
        "mngr create my-task@my-modal-box.modal --new-host --type command --no-connect --no-ensure-clean -- sleep 100985",
        comment="you can name the host using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the address syntax actually named the host: the agent "my-task"
    # must be running on a host literally named "my-modal-box" (not an
    # auto-generated name).
    listing = e2e.run(
        'mngr list --fields "name,host.name"',
        comment="confirm the agent landed on the named host",
    )
    expect(listing).to_succeed()
    expect(listing.stdout).to_contain("my-task")
    expect(listing.stdout).to_contain("my-modal-box")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_volume(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
    mngr create my-task --provider modal -b volume=my-data:/data
    """)
    # The tutorial relies on a user-configured default agent type; the isolated
    # e2e fixture has none, so we pin `--type command` with a long-lived `sleep`
    # as the command (mirroring the other command-typed Modal tests in this
    # file). The volume build arg is host-level and independent of agent type.
    result = e2e.run(
        "mngr create my-task --provider modal -b volume=my-data:/data --type command --no-connect --no-ensure-clean -- sleep 100981",
        comment="you can mount persistent Modal volumes",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the volume actually mounted: write a file through /data and read it
    # back (confirms it is writable), and confirm /data lives on a different
    # filesystem than root (confirms a real volume mount took effect, not just
    # an empty directory left behind by a failed mount).
    probe = e2e.run(
        "mngr exec my-task 'echo persistent-data > /data/probe.txt"
        " && cat /data/probe.txt"
        ' && if [ "$(stat -c %d /data)" != "$(stat -c %d /)" ];'
        " then echo VOLUME_MOUNTED; else echo VOLUME_NOT_SEPARATE; fi'",
        comment="verify the persistent volume is mounted read-write at /data",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(probe).to_succeed()
    expect(probe.stdout).to_contain("persistent-data")
    expect(probe.stdout).to_contain("VOLUME_MOUNTED")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_target_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the target path where the agent's work directory will be mounted:
    mngr create my-task@.modal:/workspace
    """)
    # The isolated e2e environment configures no default agent type, so an
    # explicit `--type command -- sleep <N>` stands in for the tutorial's
    # implied default agent (matching the convention in test_create_basic.py).
    result = e2e.run(
        "mngr create my-task@.modal:/workspace --no-connect --no-ensure-clean --type command -- sleep 100993",
        comment="you can specify the target path where the agent's work directory will be mounted",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the agent's work directory was actually mounted at the requested
    # target path -- running in that directory is the whole point of the :PATH
    # address suffix, so checking `pwd` is stronger than asserting create exited 0.
    pwd_result = e2e.run(
        "mngr exec my-task pwd",
        comment="verify the agent's work directory is mounted at the target path",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(pwd_result).to_succeed()
    # `mngr exec` appends a trailing "Command succeeded on agent ..." status
    # line to stdout, so assert on the first (pwd) line rather than the whole
    # output.
    first_line = pwd_result.stdout.strip().splitlines()[0]
    expect(first_line).to_equal("/workspace")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_upload_and_extra_provision_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can upload files and run custom commands during host provisioning:
        mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo"
        # (--sudo-command runs as root)
    """)
    # `pip install foo` from the tutorial would fail at provision time (no such
    # package), so the test substitutes a harmless command but still exercises
    # the upload-file + extra-provision-command flags end to end.
    # `--type claude` is supplied explicitly because the isolated test profile
    # has no default `commands.create.type` configured (the tutorial relies on a
    # default set earlier in the doc). The tutorial block above is kept verbatim.
    #
    # Seed the local ~/.ssh/config with a recognizable marker so we can later
    # confirm that --upload-file transferred its *contents* (not just an empty
    # file) to the remote host. The marker is written as an SSH-config *comment*
    # line (leading '#'): the uploaded file lands at /root/.ssh/config, which
    # mngr's own SSH client parses when connecting to the host, so arbitrary
    # non-comment text there breaks the connection. The extra-provision-command
    # writes a sentinel file so we can confirm the command actually executed
    # during provisioning.
    upload_marker = "MNGR_UPLOAD_MARKER_abc123"
    provision_sentinel = "/root/mngr-provision-marker.txt"
    e2e.run(
        f"mkdir -p ~/.ssh && printf '# {upload_marker}\\n' > ~/.ssh/config",
        comment="create ssh config with a marker for the upload test",
    )
    result = e2e.run(
        "mngr create my-task --type claude --provider modal "
        "--upload-file ~/.ssh/config:/root/.ssh/config "
        f'--extra-provision-command "echo provisioned > {provision_sentinel}" '
        "--no-connect --no-ensure-clean",
        comment="you can upload files and run custom commands during host provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify --upload-file actually placed the file (with its contents) at the
    # requested remote path.
    uploaded = e2e.run(
        "mngr exec my-task 'cat /root/.ssh/config'",
        comment="verify the uploaded file landed on the remote host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(uploaded).to_succeed()
    expect(uploaded.stdout).to_contain(upload_marker)

    # Verify the extra provision command ran during host provisioning.
    provisioned = e2e.run(
        f"mngr exec my-task 'cat {provision_sentinel}'",
        comment="verify the extra provision command ran during provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(provisioned).to_succeed()
    expect(provisioned.stdout).to_contain("provisioned")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_no_start_on_boot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, agents are started when a host is booted. This can be disabled:
    mngr create my-task --provider modal --no-start-on-boot
    # but it only makes sense to do this if you are running multiple agents on the same host
    # that's because hosts are automatically stopped when they have no more running agents, so you have to have at least one.
    """)
    # The tutorial relies on the user's configured default agent type; the
    # isolated test environment sets none, so pass an explicit --type. We use
    # the built-in `command` type (with a long sleep) rather than claude so the
    # test exercises --no-start-on-boot without paying for claude installation
    # or depending on Anthropic credentials -- the same substitution other
    # fast modal tests in this file use.
    result = e2e.run(
        "mngr create my-task --provider modal --type command --no-start-on-boot --no-connect --no-ensure-clean -- sleep 100981",
        comment="by default, agents are started when a host is booted; this can be disabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the flag actually took effect, not just that create exited 0: the
    # created agent must be recorded with start_on_boot disabled. The CEL
    # include filter evaluates the stored boolean directly, so the agent shows
    # up under `start_on_boot == false` and is absent under `start_on_boot == true`.
    listing = e2e.run(
        "mngr list --include 'start_on_boot == false' --format '{name}'",
        comment="verify the agent was created with start-on-boot disabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(listing).to_succeed()
    expect(listing.stdout).to_contain("my-task")
    excluded = e2e.run(
        "mngr list --include 'start_on_boot == true' --format '{name}'",
        comment="confirm the agent does not appear when filtering for start-on-boot enabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(excluded).to_succeed()
    expect(excluded.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_pass_host_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also set host-level environment variables (separate from agent env vars):
    mngr create my-task --provider modal --pass-host-env MY_VAR
    # --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)
    """)
    # The agent type is supplied as `command` (running `sleep`) so the test does
    # not depend on a default agent type being configured or on claude being
    # installed/authenticated on the remote host; `--pass-host-env` is exercised
    # the same way regardless of agent type.
    result = e2e.run(
        "MY_VAR=hello mngr create my-task --provider modal --pass-host-env MY_VAR --type command --no-connect --no-ensure-clean -- sleep 100529",
        comment="you can also set host-level environment variables",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the host-level env var actually reached the remote host. `mngr exec`
    # sources the host env file before running the command, so a host env var
    # forwarded via --pass-host-env (MY_VAR=hello in the launching shell) must be
    # visible here. This confirms the value was propagated, not just accepted.
    env_result = e2e.run(
        "mngr exec my-task 'echo HOST_MY_VAR=$MY_VAR'",
        comment="verify the host-level env var is set on the remote host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("HOST_MY_VAR=hello")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.flaky
@pytest.mark.timeout(420)
def test_create_modal_reuse(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mngr create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.
    """)
    # The tutorial relies on the user's configured default agent type; the
    # isolated test environment has many agent-type plugins installed and no
    # default, so pass an explicit --type. We substitute the lightweight
    # command agent (running a sleep) for the tutorial's default agent: the
    # claude provisioning path performs many SSH file operations that are prone
    # to transient "SSH protocol banner" failures, and this test creates the
    # agent twice. --format json lets us capture the resolved agent_id so we
    # can assert the second call truly reuses the existing agent.
    reuse_command = (
        "mngr create sisyphus --reuse --provider modal --type command"
        " --no-connect --no-ensure-clean --format json -- sleep 100982"
    )
    # First invocation: the agent does not exist yet, so it is created.
    first = e2e.run(
        reuse_command,
        comment="another handy trick is to make the create command idempotent (first call creates the agent)",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(first).to_succeed()
    first_agent_id = json.loads(first.stdout)["agent_id"]

    # Second invocation with --reuse: the agent already exists, so it is reused
    # (and started) instead of creating a duplicate.
    second = e2e.run(
        reuse_command,
        comment="if that agent already exists, it will be reused (and started) instead of creating a new one",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(second).to_succeed()
    second_agent_id = json.loads(second.stdout)["agent_id"]
    assert second_agent_id == first_agent_id, (
        f"Expected --reuse to reuse the existing agent, but the agent_id changed: "
        f"{first_agent_id!r} -> {second_agent_id!r}"
    )

    # No duplicate was created: exactly one agent named "sisyphus" exists.
    list_result = e2e.run("mngr list --format json", comment="verify the agent was reused, not duplicated")
    expect(list_result).to_succeed()
    sisyphus_agents = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "sisyphus"]
    assert len(sisyphus_agents) == 1, f"Expected exactly one 'sisyphus' agent after reuse, got: {sisyphus_agents}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_basic_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # basic Modal agent (also covered in the CREATING AGENTS REMOTELY section above)
        mngr create my-task --provider modal
    """)
    # The tutorial relies on a configured default agent type (the "default
    # agent (e.g. claude)" referenced throughout the tutorial). The isolated
    # test profile has no default configured, so we pass --type claude as an
    # extra flag to exercise the canonical "basic Modal agent" the block means.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type claude --no-connect --no-ensure-clean",
            comment="basic Modal agent",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the actual effect: the agent is registered and discoverable as a
    # Modal agent. Filtering by --provider modal means a match here confirms the
    # agent was really created on a Modal host (not just that create exited 0).
    list_result = e2e.run(
        "mngr list --provider modal",
        comment="confirm the agent was created on a Modal host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


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
    # The tutorial relies on the user's configured default agent type; the
    # isolated test profile has none, so we pass --type claude explicitly
    # (claude was the source-coded default these commands assumed previously).
    result = e2e.run(
        "mngr create my-task --provider modal --type claude -b cpu=4 -b memory=16 --no-connect --no-ensure-clean",
        comment="specify CPU and memory resources (gpu omitted to avoid quota issues)",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Beyond exit code 0: confirm Modal actually accepted the cpu/memory build
    # args and brought up a functioning host by running a command on it over
    # ssh. A reachable host that returns a real path proves the build-arg'd
    # container booted, not just that create returned cleanly.
    exec_result = e2e.run(
        'mngr exec my-task "pwd"',
        comment="verify the build-arg'd host is reachable",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("/")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_custom_image_base(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use a custom Docker image as the base
        mngr create my-task --provider modal -b image=python:3.12
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b image=python:3.12 --type command "
            "--no-connect --no-ensure-clean -- sleep 100981",
            comment="use a custom Docker image as the base",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()
    # Verify the custom base image is actually in use: the python:3.12 image
    # ships Python 3.12 as its default `python`, so creating succeeds is not
    # enough -- exec into the host and confirm the interpreter version.
    version_result = e2e.run(
        "mngr exec my-task 'python --version'",
        comment="confirm the python:3.12 base image is in use on the remote host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(version_result).to_succeed()
    expect(version_result.stdout).to_contain("Python 3.12")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_custom_dockerfile_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use a custom Dockerfile
        mngr create my-task --provider modal -b file=./Dockerfile.agent
    """)
    # Write a minimal Dockerfile so the build resolves. A RUN step bakes a
    # sentinel marker file into the image; reading it back from the running
    # host below proves the custom Dockerfile's build steps actually executed
    # (i.e. the -b file= build arg was honored), rather than just that create
    # exited 0.
    expect(
        e2e.run(
            "printf 'FROM python:3.12\\nRUN echo modal-custom-dockerfile-ran"
            " > /custom-dockerfile-marker.txt\\n' > Dockerfile.agent",
            comment="write a minimal Dockerfile.agent with a build-step marker",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b file=./Dockerfile.agent --no-connect --no-ensure-clean",
            comment="use a custom Dockerfile",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()
    # Verify the custom Dockerfile was actually built and used by reading back
    # the marker that its RUN step created in the image.
    marker_result = e2e.run(
        "mngr exec my-task 'cat /custom-dockerfile-marker.txt'",
        comment="verify the custom Dockerfile's build steps ran on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("modal-custom-dockerfile-ran")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_volume_simple(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # mount a persistent volume for data that survives host destruction
        mngr create my-task --provider modal -b volume=my-data:/data
    """)
    # The tutorial omits --type because it relies on the user's configured
    # default agent type; the isolated test profile has none, so we pass an
    # explicit `--type command -- sleep` (the convention used across the e2e
    # suite) to keep a long-lived agent.
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b volume=my-data:/data"
            " --type command --no-connect --no-ensure-clean -- sleep 100460",
            comment="mount a persistent volume",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the volume was actually mounted and is usable, rather than only
    # checking that create exited 0. Modal mounts a `-b volume=NAME:/path`
    # request by symlinking the requested path into its volume store
    # (e.g. /data -> /__modal/volumes/vo-...), so we resolve the link to
    # confirm it is backed by a real Modal volume and round-trip a write to
    # confirm the mount is usable.
    volume_check = e2e.run(
        "mngr exec my-task 'readlink -f /data; "
        "echo persisted > /data/mngr_probe.txt && cat /data/mngr_probe.txt'",
        comment="verify the persistent volume is mounted and writable at /data",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(volume_check).to_succeed()
    expect(volume_check.stdout).to_contain("/__modal/volumes")
    expect(volume_check.stdout).to_contain("persisted")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_idle_timeout_120(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set an idle timeout to avoid runaway costs
        mngr create my-task --provider modal --idle-timeout 120
    """)
    # Substitute `--type command -- sleep` for the tutorial's default agent type
    # to avoid the slow Modal claude startup; the test verifies that
    # --idle-timeout 120 is accepted on create.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-timeout 120 --type command --no-connect --no-ensure-clean -- sleep 100120",
            comment="set an idle timeout to avoid runaway costs",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the idle timeout was actually applied to the host (not just that
    # the flag was accepted): the agent should report idle_timeout_seconds=120.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the idle timeout was applied",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    my_task_agents = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(my_task_agents) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    assert my_task_agents[0]["idle_timeout_seconds"] == 120, (
        f"Expected idle_timeout_seconds=120, got {my_task_agents[0]['idle_timeout_seconds']}"
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
    # The tutorial assumes a default agent type has been configured (the
    # installer sets commands.create.type). The isolated e2e profile has no
    # default, so make the setup create explicit with the auth-free `command`
    # agent type running a long sleep (the standard idiom in test_create_basic).
    expect(
        e2e.run(
            "mngr create my-task --provider modal --no-connect --no-ensure-clean --type command -- sleep 100200",
            comment="create the modal agent first",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            'mngr snapshot create my-task --name "checkpoint-1"',
            comment="create a snapshot for checkpointing",
        )
    ).to_succeed()
    # Verify the checkpoint actually exists under the name we gave it, rather
    # than relying solely on the create command's exit code.
    list_result = e2e.run(
        "mngr snapshot list my-task",
        comment="confirm the checkpoint snapshot was recorded",
    )
    expect(list_result).to_succeed()
    assert "checkpoint-1" in list_result.stdout


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_list_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all Modal agents
        mngr list --provider modal
    """)
    # Create a Modal agent first so the list has a real agent to show. Without
    # this, `mngr list --provider modal` returns "No agents found" and never
    # actually queries Modal, which both makes the assertion meaningless and
    # trips the @pytest.mark.modal resource guard (the mark requires Modal to be
    # exercised).
    expect(
        e2e.run(
            "mngr create my-task --provider modal --no-connect --no-ensure-clean",
            comment="create a Modal agent so the list has something to show",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    result = e2e.run("mngr list --provider modal", comment="list all Modal agents")
    expect(result).to_succeed()
    # The created agent should appear in the Modal-filtered listing.
    expect(result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(360)
def test_destroy_all_modal_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all Modal agents (be careful!)  Useful for cleaning up while prototyping
        mngr list --include 'host.provider == "modal"' --ids | mngr destroy - -f
    """)
    # Create a real Modal agent first so the destroy filter has something to act
    # on. Without this the pipeline runs against an empty list, which neither
    # exercises the destroy path nor invokes Modal (failing the @modal guard).
    # A `command`-type agent running `sleep` needs no AI credentials or default
    # agent type, so it works in any environment.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100980",
            comment="create a Modal agent to clean up",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Confirm the agent shows up under the Modal provider before destroying it.
    list_before = e2e.run(
        "mngr list --provider modal",
        comment="list Modal agents before destroying",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_before).to_succeed()
    expect(list_before.stdout).to_contain("my-task")
    # Destroy all Modal agents via the filter+stdin pipeline from the tutorial.
    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"modal\"' --ids | mngr destroy - -f",
            comment="destroy all Modal agents via filter+stdin",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # The agent's host is now destroyed, so it should no longer appear among the
    # active Modal agents (--active excludes destroyed/crashed/failed hosts).
    list_after = e2e.run(
        "mngr list --provider modal --active",
        comment="confirm no active Modal agents remain after destroying",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_after).to_succeed()
    expect(list_after.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_idle_timeout_60(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set an idle timeout (in seconds) -- the agent's host will stop after this much inactivity
        mngr create my-task --provider modal --idle-timeout 60
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-timeout 60 --no-connect --no-ensure-clean",
            comment="set an idle timeout (in seconds)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the timeout was actually applied to the host, not just that the
    # command exited 0: the agent should report idle_timeout_seconds == 60.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the 60s idle timeout was applied to the created agent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds == 60, got: {matching[0].get('idle_timeout_seconds')}"
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
    # Substitute `--type command -- sleep` for the implicit (claude) agent so the
    # test exercises --idle-mode ssh without paying the slow Modal claude startup.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-mode ssh --idle-timeout 300 --type command --no-connect --no-ensure-clean -- sleep 100981",
            comment="control what counts as activity with --idle-mode",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the idle mode and timeout were actually persisted on the agent,
    # not just that the create command accepted the flags.
    settings = e2e.run(
        'mngr list --format "{name}|{idle_mode}|{idle_timeout_seconds}"',
        comment="verify the idle mode and timeout were applied",
    )
    expect(settings).to_succeed()
    expect(settings.stdout).to_contain("my-task|SSH|300")


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
    # verify --idle-mode run is accepted with --type command.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --idle-mode run --idle-timeout 60 --no-connect --no-ensure-clean -- sleep 100980",
            comment="run mode stops the host when the script finishes",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the flags were actually applied to the created agent (not merely
    # accepted by the parser) by inspecting the agent metadata: it should be a
    # `command` agent running our substituted command on a Modal host, with the
    # run idle-mode and the 60s idle-timeout persisted.
    list_result = e2e.run(
        "mngr list --format json",
        comment="inspect the created agent's idle configuration",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got {matching}"
    agent = matching[0]
    assert agent["type"] == "command", agent
    assert agent["command"] == "sleep 100980", agent
    assert agent["idle_mode"] == "RUN", agent
    assert agent["idle_timeout_seconds"] == 60, agent
    assert "modal" in agent["host"]["provider_name"], agent

    # Verify the concrete effect of `--type command`: the substituted
    # long-running command is actually running on the remote host.
    ps_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="verify the long-running command is running on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 100980")


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
    # The tutorial block omits --type because a real user has a configured
    # default agent type; the isolated test env has none, so we pin
    # `--type command -- sleep N` (the established fast-agent pattern in this
    # suite). The sleep values are unique per create call so any leaked
    # process traces back to the exact command. This still exercises the
    # multiple-agents-on-one-host mechanics, which is what the block teaches.
    expect(
        e2e.run(
            "mngr create agent-1@shared-host.modal --provider modal --new-host"
            " --no-connect --no-ensure-clean --type command -- sleep 100590",
            comment="create first agent on a named host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create agent-2@shared-host.modal --no-connect --no-ensure-clean --type command -- sleep 100591",
            comment="create additional agents on the same host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # The list command from the block must succeed and show both agents.
    list_result = e2e.run(
        'mngr list --fields "name,state,host.name"',
        comment="list agents to see which share a host",
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("agent-1")
    expect(list_result.stdout).to_contain("agent-2")

    # The core promise of this block is that both agents land on the SAME host.
    # Verify it concretely by comparing the host each agent reports.
    detail_result = e2e.run("mngr list --format json", comment="inspect host assignment for both agents")
    expect(detail_result).to_succeed()
    agents_by_name = {a["name"]: a for a in json.loads(detail_result.stdout)["agents"]}
    assert {"agent-1", "agent-2"} <= set(agents_by_name), f"Expected both agents, got: {sorted(agents_by_name)}"
    assert agents_by_name["agent-1"]["host"]["name"] == agents_by_name["agent-2"]["host"]["name"], (
        "Expected both agents to share a host, but they are on different hosts: "
        f"{agents_by_name['agent-1']['host']['name']} vs {agents_by_name['agent-2']['host']['name']}"
    )
    assert agents_by_name["agent-1"]["host"]["provider_name"] == "modal"

    # Stopping one agent must not affect the other: agent-1 becomes STOPPED
    # while agent-2 keeps running, and the shared host stays up for it.
    expect(e2e.run("mngr stop agent-1", comment="stop one agent without affecting others")).to_succeed()
    after_stop_result = e2e.run("mngr list --format json", comment="verify only agent-1 stopped")
    expect(after_stop_result).to_succeed()
    after_stop_by_name = {a["name"]: a for a in json.loads(after_stop_result.stdout)["agents"]}
    assert after_stop_by_name["agent-1"]["state"] == "STOPPED", (
        f"Expected agent-1 to be STOPPED, got: {after_stop_by_name['agent-1']['state']}"
    )
    assert after_stop_by_name["agent-2"]["state"] in ("RUNNING", "WAITING"), (
        f"Expected agent-2 to still be active, got: {after_stop_by_name['agent-2']['state']}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_upload_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # upload a file to the agent's host during creation
        mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config
    """)
    # Write recognizable content so we can later confirm the upload landed
    # (and is not just an incidentally-present empty file on the host).
    expect(
        e2e.run(
            "mkdir -p ~/.ssh && printf 'Host upload-sentinel\\n    User mngr-e2e\\n' > ~/.ssh/config",
            comment="ensure ssh config exists",
        )
    ).to_succeed()
    # The tutorial relies on the user's configured default agent type; the
    # isolated test environment has none, so pin --type command -- sleep N
    # (the same convention the rest of this suite uses). The --upload-file flag
    # under test is exercised at host-provisioning time regardless of agent type.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config"
            " --type command --no-connect --no-ensure-clean -- sleep 100631",
            comment="upload a file to the agent's host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the file actually landed on the remote host at the requested path
    # with the exact content we uploaded -- a successful exit code alone does
    # not prove the upload happened.
    uploaded = e2e.run(
        "mngr exec my-task 'cat /root/.ssh/config'",
        comment="verify the uploaded file landed on the remote host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(uploaded).to_succeed()
    expect(uploaded.stdout).to_contain("Host upload-sentinel")
    expect(uploaded.stdout).to_contain("User mngr-e2e")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_pip_install(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a setup command during host provisioning
        mngr create my-task --provider modal --extra-provision-command "pip install numpy pandas"
    """)
    # Use a lightweight provision command that writes a marker file (instead of
    # pulling large pip packages) so the test stays fast but can still verify
    # that the provision command actually ran on the host.
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            ' --extra-provision-command "echo provisioned > /tmp/provision_marker.txt"'
            " --no-connect --no-ensure-clean",
            comment="run a setup command during host provisioning (substituted with a marker write)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the provision command actually executed on the host by reading back
    # the marker file it wrote -- exit code 0 alone only proves the flag parsed.
    marker_result = e2e.run(
        "mngr exec my-task 'cat /tmp/provision_marker.txt'",
        comment="confirm the provision command ran by reading the marker it wrote",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("provisioned")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_sudo_apt(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command as root during provisioning (if your default user is not root, assumes passwordless sudo for that user)
        mngr create my-task --provider modal --extra-provision-command "sudo apt-get update && apt-get install -y vim"
    """)
    # The tutorial relies on the user's configured default agent type; the
    # isolated test profile has none, so pin a lightweight command agent (matching
    # the substitution philosophy used elsewhere in this file). Instead of the
    # real apt-get (slow + costly), the substituted provision command writes a
    # sentinel to a root-owned path (/provision_marker), which both avoids the
    # apt cost and proves the command ran as root on the host.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command"
            ' --extra-provision-command "echo sudo-provisioned > /provision_marker"'
            " --no-connect --no-ensure-clean -- sleep 100982",
            comment="provision as root (substituted with a root-only file write to avoid apt cost)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the extra-provision-command actually executed on the host: the
    # sentinel file exists at a root-owned path with the expected contents.
    marker_result = e2e.run(
        'mngr exec my-task "cat /provision_marker"',
        comment="confirm the root provision command ran on the host",
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("sudo-provisioned")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_append_file(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # append content to a file on the host using a provision command
        mngr create my-task --provider modal --extra-provision-command "echo 'export PATH=/opt/bin:\$PATH' >> /root/.bashrc"
    """)
    # The tutorial omits --type because it assumes a configured default agent;
    # the isolated e2e profile has none, so pass --type command -- sleep (the
    # cheap, auth-free pattern used by the other modal tests in this file) while
    # still exercising the --extra-provision-command host-provisioning flag.
    #
    # Mirror the tutorial's "append content to a file" intent but target a
    # dedicated marker file instead of /root/.bashrc, so we can later read it
    # back to prove the provision command actually ran on the host.
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            " --extra-provision-command \"echo path-appended >> /root/provision_marker.txt\""
            " --type command --no-connect --no-ensure-clean -- sleep 100201",
            comment="append content to a file on the host using a provision command",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Verify the agent is actually running on the host and that the
    # --extra-provision-command ran: the marker file must exist with our content.
    marker = e2e.run(
        "mngr exec my-task 'cat /root/provision_marker.txt'",
        comment="confirm the provision command ran by reading the appended marker file",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(marker).to_succeed()
    expect(marker.stdout).to_contain("path-appended")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_combined_setup_steps(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # combine multiple setup steps
        mngr create my-task --provider modal \
          --upload-file ./requirements.txt:/workspace/requirements.txt \
          --extra-provision-command "sudo apt-get update && apt-get install -y build-essential" \
          --extra-provision-command "pip install -r /workspace/requirements.txt"
    """)
    expect(e2e.run("echo 'requests==2.32.0' > requirements.txt", comment="write requirements.txt")).to_succeed()
    # The tutorial's provision commands (apt-get / pip install) are substituted
    # with fast no-network commands that each drop a marker file, so we can
    # later assert that *both* repeated --extra-provision-command invocations
    # actually ran on the host (not just that the create exited 0).
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            " --upload-file ./requirements.txt:/workspace/requirements.txt"
            ' --extra-provision-command "echo step-one > /tmp/provision-marker-1"'
            ' --extra-provision-command "echo step-two > /tmp/provision-marker-2"'
            " --no-connect --no-ensure-clean",
            comment="combine upload + two extra-provision commands (substituted for speed)",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()
    # The uploaded file should have landed at the requested remote path with its
    # original contents.
    uploaded = e2e.run(
        'mngr exec my-task "cat /workspace/requirements.txt"',
        comment="verify the uploaded file landed on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(uploaded).to_succeed()
    expect(uploaded.stdout).to_contain("requests==2.32.0")
    # Both repeated provision commands should have executed during provisioning.
    markers = e2e.run(
        'mngr exec my-task "cat /tmp/provision-marker-1 /tmp/provision-marker-2"',
        comment="verify both extra-provision commands ran",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(markers).to_succeed()
    expect(markers.stdout).to_contain("step-one")
    expect(markers.stdout).to_contain("step-two")
