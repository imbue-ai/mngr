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
@pytest.mark.timeout(180)
def test_create_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also launch claude remotely in Modal:
    mngr create my-task --provider modal
    # see more details below in "CREATING AGENTS REMOTELY" for relevant options
    """)
    # The tutorial relies on a configured default agent type (claude); the e2e
    # environment sets none, so pass --type command (with a trailing command)
    # to exercise the Modal launch path without a real claude agent, matching
    # the convention used by the other create e2e tests.
    result = e2e.run(
        "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100124",
        comment="you can also launch claude remotely in Modal",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent actually landed on a Modal host, not just that the
    # command exited 0: it should be discoverable via `mngr list` and report
    # the modal provider as its host's owner.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent was created on the modal provider",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"expected exactly one 'my-task' agent, got: {agents}"
    assert matching[0]["host"]["provider_name"] == "modal", (
        f"expected agent on modal provider, got host: {matching[0]['host']}"
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
        # --type claude makes the tutorial's default agent type explicit: the
        # tutorial assumes the installer configured commands.create.type=claude,
        # but the isolated e2e profile sets no default, so we pass it directly.
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

    # Verify the agent was actually created as a persisted, discoverable effect
    # (not just that the create command exited 0): it must show up in
    # `mngr list`. The exit-0 above already proves the ready-signal handshake
    # and send_message both succeeded (wait_for_ready_signal raises on timeout
    # and send_message raises on a failed send), so this confirms the launched
    # agent is reachable on the remote Modal host afterwards.
    list_result = e2e.run(
        "mngr list",
        comment="verify the launched agent is discoverable on the remote host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
# Cold Modal provisioning in the offload (Modal-in-Modal) environment -- a fresh
# environment, a snapshot-function deploy, and an image build -- can push a plain
# --no-connect create past the old 120s budget (observed ~77s warm, >120s cold).
# Use a wider mark, kept above the run timeout below so the command's own timeout
# surfaces a clear assertion first.
@pytest.mark.timeout(300)
def test_create_modal_edit_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mngr create my-task --provider modal --edit-message
    """)
    # The tutorial block omits --type because it relies on the user's configured
    # default agent type ([commands.create] type). The isolated e2e profile has no
    # default, so we pass --type claude explicitly here -- claude is the natural
    # pairing for --edit-message (which composes the agent's initial message).
    #
    # --edit-message launches $EDITOR on a temp file. The e2e sandbox has no
    # interactive editor installed (and no TTY), so we point EDITOR at `true`,
    # which exits 0 immediately without modifying the file. That leaves the
    # message empty (nothing is sent) while still exercising the full
    # editor-spawn-and-wait code path that --edit-message adds to create.
    result = e2e.run(
        "EDITOR=true mngr create my-task --provider modal --type claude --edit-message --no-connect --no-ensure-clean",
        comment="you can also edit the message *while the agent is starting up*",
        timeout=270.0,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_rsync(e2e: E2eSession, temp_git_repo: Path) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    # rsync transfers data "beyond just the git data": gitignored files are
    # skipped by the default git-based transfer but copied by --rsync. Stage two
    # gitignored trees so we can assert both of --rsync's behaviors -- one that
    # --rsync-args excludes (node_modules) and one it should carry over.
    gitignore = temp_git_repo / ".gitignore"
    gitignore.write_text(gitignore.read_text() + "node_modules/\nrsync_only/\n")
    (temp_git_repo / "rsync_only").mkdir()
    (temp_git_repo / "rsync_only" / "payload.txt").write_text("rsync-payload-7f3a9c")
    (temp_git_repo / "node_modules").mkdir()
    (temp_git_repo / "node_modules" / "excluded.txt").write_text("should-not-transfer")

    # The tutorial relies on the user's configured default agent type; the e2e
    # fixture sets no default, so name one explicitly here. rsync is orthogonal
    # to the agent body, so use the lightweight `command` agent (matching the
    # other agent-incidental modal tests) rather than provisioning Claude.
    result = e2e.run(
        'mngr create my-task --provider modal --type command --rsync --rsync-args "--exclude=node_modules" --no-connect --no-ensure-clean -- sleep 100000',
        comment="you can use rsync to transfer extra data as well",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # rsync carried the gitignored file that the default git transfer skips,
    # proving data was transferred "beyond just the git data".
    payload = e2e.run(
        "mngr exec my-task 'cat rsync_only/payload.txt'",
        comment="rsync transfers extra (gitignored) data beyond the git data",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(payload.stdout).to_contain("rsync-payload-7f3a9c")

    # ...but it honored --rsync-args "--exclude=node_modules", so the excluded
    # tree never reached the host.
    excluded = e2e.run(
        "mngr exec my-task 'test -e node_modules && echo PRESENT || echo ABSENT'",
        comment="--rsync-args --exclude=node_modules keeps node_modules off the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(excluded.stdout).to_contain("ABSENT")


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
    result = e2e.run(
        'mngr create my-task --provider modal --no-connect --no-ensure-clean -- --dangerously-skip-permissions --append-system-prompt "Don\'t ask me any questions!"',
        comment="one of the coolest features of mngr is the ability to create agents on remote hosts",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # A clean exit only proves the passthrough args parsed; confirm the create
    # actually produced a registered remote agent (and so the trailing `--`
    # args did not consume the agent name or provider).
    list_result = e2e.run("mngr list", comment="confirm the remote agent was created", timeout=_REMOTE_TIMEOUT)
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


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

    # Verify the flag actually took effect, not just that the command exited 0:
    # the created agent's host must be configured with the 60-second idle timeout
    # that --idle-timeout requested (the value mngr uses to shut the host down).
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="confirm the agent was created with the requested idle timeout",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    my_tasks = [agent for agent in agents if agent["name"] == "my-task"]
    assert my_tasks, "expected the created 'my-task' agent in the modal listing"
    # The running host reports the configured idle timeout from its certified
    # data. A snapshot-backed host for the same agent may also appear while
    # unauthenticated, reporting None because it hasn't loaded certified data
    # yet -- so assert on the timeouts that are actually reported.
    reported_timeouts = [
        agent["idle_timeout_seconds"] for agent in my_tasks if agent["idle_timeout_seconds"] is not None
    ]
    assert reported_timeouts and all(timeout == 60 for timeout in reported_timeouts), (
        f"Expected the created agent to report idle_timeout_seconds=60, got "
        f"{[(agent['idle_timeout_seconds'], agent['host']['state']) for agent in my_tasks]}"
    )


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
    result = e2e.run(
        'mngr create my-task --provider modal --idle-mode "ssh" --no-connect --no-ensure-clean',
        comment="You can customize what inactivity means by using the --idle-mode flag",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


# Unlike the other tests in this file, this one targets an existing host that
# does not exist, so it fails fast during host lookup and never invokes the
# Modal CLI. It therefore must NOT carry @pytest.mark.modal (the resource guard
# fails a modal-marked test that never invokes modal).
@pytest.mark.release
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    # The tutorial command relies on the user having a default agent type
    # configured; the isolated e2e profile has none, so we pass --type
    # explicitly to reach the existing-host lookup that this test exercises.
    result = e2e.run(
        "mngr create my-task@my-dev-box --type claude --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
    )
    expect(result).to_fail()
    # The host does not exist, so creation must fail with a host-not-found error.
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)could not find host|host.*not found|no.*host|unknown.*host|not.*registered")
    # The error must name the specific host that could not be found.
    expect(combined).to_contain("my-dev-box")


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
    # The tutorial block omits --type because it assumes the user has configured
    # claude as their default agent type. Since the e2e fixture sets no default,
    # we pass --type explicitly here (an allowed extra flag); claude is the type
    # the tutorial implies and the historical default before it moved to config.
    result = e2e.run(
        "mngr create my-task --provider modal --type claude -b cpu=4 -b memory=16 -b image=python:3.12 --no-connect --no-ensure-clean",
        comment="build arguments let you customize that new remote host",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_succeed()

    # Verify the `image=python:3.12` build arg actually took effect rather than
    # merely that the command exited 0: the python:3.12 base image ships Python
    # 3.12 as its system interpreter, so reading it back proves the custom image
    # was used to build the host. (cpu/memory aren't reliably observable from
    # inside a Modal container, so we don't assert on them.)
    python_version = e2e.run(
        'mngr exec my-task "python --version"',
        comment="verify the image build arg produced a python:3.12 host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(python_version).to_succeed()
    expect(python_version.stdout).to_contain("Python 3.12")


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
    # Create the Dockerfile and context directory so the build args have real targets
    e2e.run(
        "echo 'FROM python:3.12-slim' > Dockerfile.agent && mkdir -p agent-context",
        comment="create Dockerfile and context",
    )
    result = e2e.run(
        "mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context --no-connect --no-ensure-clean",
        comment="the most important build args for Modal are --file and --context-dir",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # Verify the custom Dockerfile was actually used (not silently ignored):
    # the python:3.12-slim base lacks tools that mngr's default image bundles
    # (openssh-server, tmux, etc.), so provisioning emits "not pre-installed in
    # the base image" warnings and installs them at runtime. These only appear
    # when the host is built from the custom -b file= image.
    expect(result.stdout + result.stderr).to_match(r"(?i)not pre-installed in the base image")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_named_host_new_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can name the host using the address syntax:
    mngr create my-task@my-modal-box.modal --new-host
    # (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)
    """)
    # The tutorial block assumes claude is the configured default agent type
    # (see the "when claude is your default agent type" note near the top of
    # mega_tutorial.sh). The isolated e2e profile does not set a default, so we
    # pass --type claude explicitly here to mirror that documented premise.
    result = e2e.run(
        "mngr create my-task@my-modal-box.modal --new-host --type claude --no-connect --no-ensure-clean",
        comment="you can name the host using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # The whole point of this block is naming the host via the address syntax,
    # so verify the concrete effect: the agent must be named "my-task" and it
    # must live on a Modal host actually named "my-modal-box" (not an
    # auto-generated host name).
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent landed on the host named via the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}: {parsed['agents']}"
    agent = matching[0]
    assert agent["type"] == "claude", f"Expected agent type 'claude', got {agent['type']!r}"
    assert agent["host"]["name"] == "my-modal-box", (
        f"Expected host named 'my-modal-box', got {agent['host']['name']!r}"
    )
    assert agent["host"]["provider_name"] == "modal", (
        f"Expected provider 'modal', got {agent['host']['provider_name']!r}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_volume(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
    mngr create my-task --provider modal -b volume=my-data:/data
    """)
    # --type claude is an extra flag (not in the tutorial block): the isolated
    # e2e fixture has no configured default agent type, whereas the tutorial
    # assumes the user's default (stored under [commands.create] type) is set.
    result = e2e.run(
        "mngr create my-task --provider modal --type claude -b volume=my-data:/data --no-connect --no-ensure-clean",
        comment="you can mount persistent Modal volumes",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the actual effect of the volume build arg, not just exit code 0.
    # Modal mounts named volumes under /__modal/volumes and exposes the requested
    # target path (/data) as a symlink into that mount, so `readlink -f /data`
    # resolving under /__modal/volumes proves the named volume is attached there.
    # Writing and reading back a probe file additionally confirms the volume is a
    # real, writable filesystem at that path (not a missing or read-only dir).
    volume_result = e2e.run(
        "mngr exec my-task 'readlink -f /data; echo persisted-data > /data/probe.txt && cat /data/probe.txt'",
        comment="verify the persistent volume is mounted and writable at /data",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(volume_result).to_succeed()
    expect(volume_result.stdout).to_contain("/__modal/volumes")
    expect(volume_result.stdout).to_contain("persisted-data")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
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

    # Verify the :PATH suffix actually set the agent's work directory on the
    # remote host, rather than just being accepted by the parser. The target
    # path is used verbatim as the work_dir (see _resolve_transfer_target).
    list_result = e2e.run("mngr list --format json", comment="Verify the agent's work_dir is the target path")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one agent named 'my-task', got: {agents}"
    assert matching[0]["work_dir"] == "/workspace", (
        f"Expected work_dir to be the target path '/workspace', got: {matching[0]['work_dir']}"
    )


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_upload_and_extra_provision_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can upload files and run custom commands during host provisioning:
    mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "echo provisioned"
    """)
    # Create ~/.ssh/config so the upload-file flag has a real file to work with
    e2e.run("mkdir -p ~/.ssh && touch ~/.ssh/config", comment="create ssh config for upload test")
    # The extra-provision-command writes a sentinel file so its effect is
    # observable on the host afterwards (a bare `echo` leaves no trace to assert
    # on). Both features under test (file upload, provision command) are then
    # verified by inspecting the remote host below.
    result = e2e.run(
        "mngr create my-task --provider modal --type claude"
        " --upload-file ~/.ssh/config:/root/.ssh/config"
        ' --extra-provision-command "echo provisioned > /root/provision_marker.txt"'
        " --no-connect --no-ensure-clean",
        comment="you can upload files and run custom commands during host provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the uploaded file actually landed at its target path on the host.
    upload_check = e2e.run(
        "mngr exec my-task 'test -f /root/.ssh/config && echo UPLOAD_OK || echo UPLOAD_MISSING'",
        comment="verify the uploaded file landed at its target path",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(upload_check).to_succeed()
    expect(upload_check.stdout).to_contain("UPLOAD_OK")

    # Verify the extra-provision-command ran during provisioning by reading the
    # sentinel file it wrote.
    provision_check = e2e.run(
        "mngr exec my-task 'cat /root/provision_marker.txt'",
        comment="verify the extra-provision-command ran during provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(provision_check).to_succeed()
    expect(provision_check.stdout).to_contain("provisioned")


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
    # The tutorial command relies on the user's configured default agent type;
    # the e2e fixture has none, so we pin --type command (a lightweight stand-in
    # that needs no remote runtime install) with a sleep command. The agent is
    # never started here (--no-start-on-boot), so the command itself never runs.
    result = e2e.run(
        "mngr create my-task --provider modal --no-start-on-boot --no-connect --no-ensure-clean --type command -- sleep 100530",
        comment="by default, agents are started when a host is booted; this can be disabled",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the effect of --no-start-on-boot: the created agent is recorded with
    # start_on_boot disabled, so it would not be restarted if its host rebooted.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent was created with start-on-boot disabled",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one my-task agent, got: {agents}"
    assert matching[0]["start_on_boot"] is False, (
        f"Expected start_on_boot to be disabled, got: {matching[0]['start_on_boot']}"
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

    # Verify the host-level env var was actually forwarded: commands run on the
    # host source the host env file, so MY_VAR should resolve to the value taken
    # from the shell at create time.
    env_result = e2e.run(
        "mngr exec my-task 'printenv MY_VAR'",
        comment="host-level env vars are available to commands run on the host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("hello")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_create_modal_reuse(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mngr create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.
    """)
    # The e2e fixture sets no default agent type, so --type command -- sleep N
    # stands in for the real claude agent (matching test_create_basic.py). The
    # sleep keeps the agent alive so the host stays up for the reuse call below.
    create_command = (
        "mngr create sisyphus --reuse --provider modal --type command --no-connect --no-ensure-clean -- sleep 100200"
    )
    # First invocation: the agent does not exist yet, so --reuse creates it.
    first = e2e.run(
        create_command,
        comment="another handy trick is to make the create command idempotent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(first).to_succeed()

    # Second invocation of the identical command: the agent now exists, so
    # --reuse must reuse (and start) it rather than erroring or creating a
    # duplicate. This is the actual idempotency guarantee the tutorial promises.
    second = e2e.run(
        create_command,
        comment="if that agent already exists, it will be reused instead of creating a new one",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(second).to_succeed()

    # Verify the concrete effect: exactly one agent named "sisyphus" exists.
    # Idempotency means the second create did not spawn a duplicate.
    listing = e2e.run("mngr list --format json", comment="verify only one sisyphus agent exists")
    expect(listing).to_succeed()
    agents = json.loads(listing.stdout)["agents"]
    sisyphus_agents = [agent for agent in agents if agent["name"] == "sisyphus"]
    assert len(sisyphus_agents) == 1, f"Expected exactly one 'sisyphus' agent after --reuse, got: {agents}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_retry(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control connection retries and timeouts via settings.toml:
    # [retry]
    # connect_retry_times = 5
    # connect_retry_delay = "10s"
    # (--reconnect / --no-reconnect controls auto-reconnect on disconnect)
    """)
    # The tutorial block is pure commentary (retry settings live in
    # settings.toml), so we exercise a representative remote create command.
    # An explicit `--type command` is required because `mngr create` no longer
    # has a source-coded default agent type, and the isolated e2e profile does
    # not configure one. A long-running `sleep` keeps the agent alive without
    # needing Claude Code installation or auth on the remote host.
    result = e2e.run(
        "mngr create my-task --provider modal --no-connect --no-ensure-clean --type command -- sleep 100200",
        comment="retry settings are configured via [retry] in settings.toml",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the create actually took effect: the agent should appear in the
    # listing and be hosted on a Modal provider (not just exit 0).
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent was created on a Modal host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    provider_name = matching[0]["host"]["provider_name"]
    assert "modal" in provider_name.lower(), f"Expected a Modal host, got provider_name={provider_name!r}"
