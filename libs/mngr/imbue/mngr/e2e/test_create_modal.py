"""Tests for Modal agent creation from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block. This makes it
easy to maintain the mapping between tutorial content and test coverage via the
tutorial_matcher script.
"""

import json
from pathlib import Path

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
@pytest.mark.timeout(120)
def test_create_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also launch claude remotely in Modal:
    mngr create my-task --provider modal
    # see more details below in "CREATING AGENTS REMOTELY" for relevant options
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --no-connect --no-ensure-clean",
        comment="you can also launch claude remotely in Modal",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent was actually created on the modal provider",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one agent named 'my-task', got: {agents}"
    assert matching[0]["host"]["provider_name"] == "modal", (
        f"Expected host provider 'modal', got: {matching[0]['host']['provider_name']}"
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
        'MNGR_AGENT_READY_TIMEOUT=540 mngr create my-task --provider modal --no-connect --pass-env ANTHROPIC_API_KEY --message "Speed up one of my tests and make a PR on github" --no-ensure-clean',
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
    # Verify the create output confirms the initial message was actually sent.
    # This is the core behavior being demonstrated by the tutorial block --
    # exit code 0 alone wouldn't prove the message path ran.
    expect(result.stderr).to_contain("Sending initial message")

    # Verify the agent was actually created and is discoverable via mngr list.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify agent created with initial message",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected one 'my-task' agent, found {len(matching)}: {agents}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_edit_message(e2e: E2eSession, tmp_path: Path) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mngr create my-task --provider modal --edit-message
    """)
    # The CI image has no vim/vi/nano, so point EDITOR at a fake editor that
    # writes a known message into the temp file. Using real content (rather
    # than `EDITOR=true`) exercises the happy path where the edited message
    # is sent to the agent, which is the behavior the tutorial demonstrates.
    editor_script = tmp_path / "fake_editor.sh"
    editor_message = "Speed up one of my tests and make a PR on github"
    editor_script.write_text(f'#!/bin/bash\nprintf %s "{editor_message}" > "$1"\n')
    editor_script.chmod(0o755)
    result = e2e.run(
        f"EDITOR={editor_script} mngr create my-task --provider modal --edit-message --no-connect --no-ensure-clean",
        comment="you can also edit the message *while the agent is starting up*",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # The "Sending edited message..." log only fires when the editor returns
    # non-empty content, so its presence is what proves the edit-and-send flow
    # actually ran (rather than silently skipping the send on empty content).
    expect(result.stdout + result.stderr).to_contain("Sending edited message")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_rsync(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    # Gitignored payloads are only reachable through the user-level rsync step
    # (git-aware extra-file transfer skips them), so they isolate the effect
    # of --rsync and --rsync-args:
    #   data.bin       -- gitignored; --rsync should still transfer it
    #   node_modules/  -- gitignored; --rsync-args "--exclude=node_modules" should skip it
    e2e.run(
        "printf 'node_modules/\\ndata.bin\\n' >> .gitignore"
        " && echo 'rsync-only payload' > data.bin"
        " && mkdir -p node_modules"
        " && echo 'should-be-excluded' > node_modules/marker.txt",
        comment="stage gitignored data.bin and node_modules dir; only the user-level rsync sees them",
    )
    result = e2e.run(
        'mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules" --no-connect --no-ensure-clean',
        comment="you can use rsync to transfer extra data as well",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # --rsync should have copied data.bin -- without it, gitignored files stay local
    rsync_result = e2e.run(
        "mngr exec my-task 'cat data.bin'",
        comment="verify --rsync transferred the gitignored data file to the remote work dir",
        timeout=60.0,
    )
    expect(rsync_result).to_succeed()
    expect(rsync_result.stdout).to_contain("rsync-only payload")

    # --rsync-args "--exclude=node_modules" should keep that directory off the remote
    exclude_result = e2e.run(
        "mngr exec my-task 'test -d node_modules && echo PRESENT || echo EXCLUDED'",
        comment="verify --rsync-args excluded node_modules from the transfer",
        timeout=60.0,
    )
    expect(exclude_result).to_succeed()
    expect(exclude_result.stdout).to_contain("EXCLUDED")


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
    # Verify the passthrough args actually reached the agent's assembled
    # command (not just that the create CLI exited 0). The agent state
    # data.json stores the assembled command including agent_args.
    state_result = e2e.run(
        "mngr file get my-task data.json --relative-to state",
        comment="read the agent state to verify the passthrough args reached the agent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(state_result).to_succeed()
    expect(state_result.stdout).to_contain("--dangerously-skip-permissions")
    expect(state_result.stdout).to_contain("--append-system-prompt")
    # Distinct fragment of the system prompt that survives shell-quote
    # escaping of the apostrophe in "Don't" (the assembled command stores
    # the prompt as a shell-escaped string, so "Don't" appears as
    # 'Don'\"'\"'t' rather than the original literal).
    expect(state_result.stdout).to_contain("ask me any questions")


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
    result = e2e.run(
        "mngr create my-task --provider modal --idle-timeout 60 --no-connect --no-ensure-clean",
        comment="mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the idle timeout was actually applied (not just that the command succeeded).
    list_result = e2e.run("mngr list --format json", comment="Verify --idle-timeout was applied to the agent")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    assert matching[0]["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds=60, got {matching[0]['idle_timeout_seconds']}"
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

    # Verify the created agent actually has idle_mode=SSH applied on the host's
    # activity config (not just that the flag parsed successfully).
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify idle_mode was applied to the created agent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one my-task agent, got: {parsed['agents']}"
    assert matching[0]["idle_mode"] == "SSH", (
        f"Expected idle_mode='SSH' but got {matching[0].get('idle_mode')!r}; agent: {matching[0]}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    result = e2e.run(
        "mngr create my-task@my-dev-box --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_fail()
    # The error should mention the host not being found
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)host.*not found|no.*host|unknown.*host|could not find.*host|not.*registered")


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

    # Verify -b image=python:3.12 actually took effect. The default Modal base
    # is debian:bookworm-slim, which has no `python` binary at all -- only an
    # explicit python:3.12 image puts a 3.12.x interpreter at the bare `python`
    # path. So a successful `python --version` reporting 3.12 is strong
    # evidence that the image build arg was threaded through to the sandbox.
    python_version = e2e.run(
        "mngr exec my-task 'python --version'",
        comment="verify image build arg was applied (python:3.12 base)",
    )
    expect(python_version).to_succeed()
    combined = python_version.stdout + python_version.stderr
    expect(combined).to_match(r"Python 3\.12\.")


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
    # Create a Dockerfile that COPYs a marker file out of the build context.
    # The python:3.12-slim base proves -b file= took effect (the default Modal
    # image is not slim/3.12), and the COPY of context-marker.txt proves
    # -b context-dir= was used to evaluate the COPY instruction.
    e2e.run(
        "mkdir -p agent-context && "
        "echo 'context-was-used' > agent-context/context-marker.txt && "
        "printf 'FROM python:3.12-slim\\nCOPY context-marker.txt /context-marker.txt\\n' > Dockerfile.agent",
        comment="create Dockerfile and context",
    )
    result = e2e.run(
        "mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context --no-connect --no-ensure-clean",
        comment="the most important build args for Modal are --file and --context-dir",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify -b file= really used our Dockerfile (custom python 3.12-slim base).
    python_result = e2e.run(
        "mngr exec my-task 'python --version'",
        comment="verify the agent runs on the custom Dockerfile's python",
        timeout=60.0,
    )
    expect(python_result).to_succeed()
    expect(python_result.stdout).to_match(r"Python 3\.12\.")
    # Verify -b context-dir= was used to resolve the COPY at build time.
    marker_result = e2e.run(
        "mngr exec my-task 'cat /context-marker.txt'",
        comment="verify the COPY from agent-context made it into the image",
        timeout=60.0,
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("context-was-used")


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
    result = e2e.run(
        "mngr create my-task@my-modal-box.modal --new-host --no-connect --no-ensure-clean",
        comment="you can name the host using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent and host both carry the names from the address syntax,
    # rather than auto-generated names.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify agent and host names match the address syntax",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}: {parsed['agents']!r}"
    assert (
        matching[0]["host"]["name"] == "my-modal-box"
    ), f"Expected host name 'my-modal-box', got {matching[0]['host']['name']!r}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_volume(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
    mngr create my-task --provider modal -b volume=my-data:/data
    """)
    result = e2e.run(
        "mngr create my-task --provider modal -b volume=my-data:/data --no-connect --no-ensure-clean",
        comment="you can mount persistent Modal volumes",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the volume is actually mounted at /data on the remote host -- a
    # bare create that "succeeds" but doesn't mount the volume would still pass
    # to_succeed(), so we check the concrete effect via mngr exec.
    mount_result = e2e.run(
        "mngr exec my-task 'test -d /data && echo data-dir-exists'",
        comment="verify the volume mount path exists on the remote host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(mount_result).to_succeed()
    expect(mount_result.stdout).to_contain("data-dir-exists")

    # Verify the mount is writable -- Modal volumes are read-write by default
    # and the tutorial pitches them as a way to "share data between hosts".
    write_result = e2e.run(
        "mngr exec my-task 'echo hello > /data/test.txt && cat /data/test.txt'",
        comment="verify the volume is writable and readable",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(write_result).to_succeed()
    expect(write_result.stdout).to_contain("hello")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_target_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the target path where the agent's work directory will be mounted:
    mngr create my-task@.modal:/workspace
    """)
    result = e2e.run(
        "mngr create my-task@.modal:/workspace --no-connect --no-ensure-clean",
        comment="you can specify the target path where the agent's work directory will be mounted",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify that the agent's work directory was actually mounted at /workspace
    # (the whole point of the :/workspace target path syntax).
    pwd_result = e2e.run(
        "mngr exec my-task pwd",
        comment="verify the agent's work directory is at the requested target path",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(pwd_result).to_succeed()
    # mngr exec appends a status line ("Command succeeded on agent ...") after
    # the remote command's stdout, so match the first line rather than the full
    # stdout.
    expect(pwd_result.stdout.splitlines()[0]).to_equal("/workspace")


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
    # Write a known marker into ~/.ssh/config so we can verify the upload landed
    # on the remote host with the expected contents (the tutorial's command just
    # touches an empty file, which would not let us distinguish a successful
    # upload from a no-op). The marker is written as an SSH config comment so
    # that paramiko -- which the Modal backend uses to connect to the new host
    # via this same ~/.ssh/config file -- can still parse it.
    # We also diverge from the tutorial's "pip install foo" (which would fail
    # because "foo" is not a real package) by running a provision command that
    # writes a marker file, so we can assert the command actually ran during
    # provisioning.
    upload_marker = "mngr-upload-marker-line"
    provision_marker_path = "/tmp/mngr-provision-marker.txt"
    provision_marker = "mngr-provision-marker-line"
    e2e.run(
        f"mkdir -p ~/.ssh && printf '# %s\\n' '{upload_marker}' > ~/.ssh/config",
        comment="create ssh config with a known marker for upload test",
    )
    result = e2e.run(
        f"mngr create my-task --provider modal"
        f" --upload-file ~/.ssh/config:/root/.ssh/config"
        f' --extra-provision-command "echo {provision_marker} > {provision_marker_path}"'
        f" --no-connect --no-ensure-clean",
        comment="you can upload files and run custom commands during host provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify --upload-file actually delivered the file (and its content) to the
    # remote host at the requested destination.
    upload_result = e2e.run(
        "mngr exec my-task 'cat /root/.ssh/config'",
        comment="verify --upload-file delivered the file to the remote",
    )
    expect(upload_result).to_succeed()
    expect(upload_result.stdout).to_contain(upload_marker)

    # Verify --extra-provision-command actually ran during provisioning, by
    # reading the marker file that the command wrote.
    provision_result = e2e.run(
        f"mngr exec my-task 'cat {provision_marker_path}'",
        comment="verify --extra-provision-command ran during provisioning",
    )
    expect(provision_result).to_succeed()
    expect(provision_result.stdout).to_contain(provision_marker)


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
    result = e2e.run(
        "mngr create my-task --provider modal --no-start-on-boot --no-connect --no-ensure-clean",
        comment="by default, agents are started when a host is booted; this can be disabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent was actually registered and that --no-start-on-boot was
    # applied -- the observable effect of the flag is start_on_boot=False on
    # the agent's certified data, surfaced through `mngr list`.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent is registered with start_on_boot=False",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {parsed['agents']}"
    assert matching[0]["start_on_boot"] is False, (
        f"Expected start_on_boot=False for --no-start-on-boot, got: {matching[0]['start_on_boot']}"
    )


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
    result = e2e.run(
        "MY_VAR=hello mngr create my-task --provider modal --pass-host-env MY_VAR --no-connect --no-ensure-clean",
        comment="you can also set host-level environment variables",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify MY_VAR was actually propagated to the host's env file by
    # exec'ing a printenv on the remote host. mngr exec sources the host
    # env file before running the command (see build_source_env_shell_commands),
    # so a non-empty printenv result confirms --pass-host-env worked end-to-end.
    exec_result = e2e.run(
        "mngr exec my-task 'printenv MY_VAR'",
        comment="Verify MY_VAR was forwarded into the host environment",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("hello")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_reuse(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mngr create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.
    """)
    first = e2e.run(
        "mngr create sisyphus --reuse --provider modal --no-connect --no-ensure-clean",
        comment="another handy trick is to make the create command idempotent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(first).to_succeed()

    # Re-run the same idempotent command: this should reuse the existing agent
    # rather than create a duplicate. This is the behavior --reuse is for, so
    # exercise it explicitly rather than only validating the create-path branch.
    second = e2e.run(
        "mngr create sisyphus --reuse --provider modal --no-connect --no-ensure-clean",
        comment="re-running the same command reuses the existing agent instead of creating a new one",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(second).to_succeed()
    expect(second.stderr).to_contain("Reusing existing agent: sisyphus")

    # Confirm idempotency at the data level: exactly one sisyphus agent exists.
    list_result = e2e.run("mngr list --format json", comment="Verify only one sisyphus agent exists")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    sisyphus_agents = [a for a in parsed["agents"] if a["name"] == "sisyphus"]
    assert len(sisyphus_agents) == 1, f"expected exactly one sisyphus agent, got {len(sisyphus_agents)}: {sisyphus_agents}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_retry(e2e: E2eSession, project_config_dir: Path) -> None:
    e2e.write_tutorial_block("""
    # you can control connection retries and timeouts via settings.toml:
    # [retry]
    # connect_retry_times = 5
    # connect_retry_delay = "10s"
    # (--reconnect / --no-reconnect controls auto-reconnect on disconnect)
    """)
    # Drop the documented [retry] block into a project settings.toml so the
    # test actually exercises the config surface the tutorial describes
    # (instead of just running a vanilla create).
    (project_config_dir / "settings.toml").write_text(
        '[retry]\nconnect_retry_times = 5\nconnect_retry_delay = "10s"\n'
    )
    get_times = e2e.run(
        "mngr config get retry.connect_retry_times",
        comment="verify retry.connect_retry_times is loaded from settings.toml",
    )
    expect(get_times).to_succeed()
    assert get_times.stdout.strip() == "5", get_times.stdout
    get_delay = e2e.run(
        "mngr config get retry.connect_retry_delay",
        comment="verify retry.connect_retry_delay is loaded from settings.toml",
    )
    expect(get_delay).to_succeed()
    assert get_delay.stdout.strip() == "10s", get_delay.stdout
    result = e2e.run(
        "mngr create my-task --provider modal --no-connect --no-ensure-clean",
        comment="retry settings are configured via [retry] in settings.toml",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
