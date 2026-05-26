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
@pytest.mark.timeout(120)
def test_create_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also launch your default agent remotely in Modal:
    mngr create my-task --provider modal
    # see more details below in "CREATING AGENTS REMOTELY" for relevant options
    """)
    # The tutorial command relies on a user-configured default agent type,
    # which install.sh sets up for real users. Mirror that precondition here.
    expect(
        e2e.run(
            "mngr config set commands.create.type claude --scope user",
            comment="set the default agent type (normally done by install.sh)",
        )
    ).to_succeed()
    result = e2e.run(
        "mngr create my-task --provider modal --no-connect --no-ensure-clean",
        comment="you can also launch your default agent remotely in Modal",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent was actually created on Modal -- not just that the
    # command exited 0. This guards against silent regressions where create
    # would succeed without spawning anything on the remote provider.
    list_result = e2e.run("mngr list --format json", comment="Verify agent landed on Modal")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {[a['name'] for a in agents]}"
    assert matching[0]["host"]["provider_name"] == "modal", (
        f"Expected agent on modal provider, got: {matching[0]['host']['provider_name']}"
    )
    assert matching[0]["type"] == "claude", f"Expected agent type 'claude' (the configured default), got: {matching[0]['type']}"


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
    # The tutorial omits --type because the user would have a default agent
    # type configured; the e2e test environment doesn't set one, so we pass
    # --type claude explicitly here.
    result = e2e.run(
        'MNGR_AGENT_READY_TIMEOUT=540 mngr create my-task --type claude --provider modal --no-connect --pass-env ANTHROPIC_API_KEY --message "Speed up one of my tests and make a PR on github" --no-ensure-clean',
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
    # Verify the create output confirms the message dispatch step actually
    # happened (and the test isn't just passing because create exited 0 for
    # an unrelated reason).
    expect(result.stderr).to_contain("Sending initial message")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_edit_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mngr create my-task --provider modal --edit-message
    """)
    # EDITOR=true is a no-op editor that exits immediately with success,
    # so --edit-message doesn't block on interactive vim/nano input.
    result = e2e.run(
        "EDITOR=true mngr create my-task --provider modal --edit-message --type command --no-connect --no-ensure-clean -- sleep 100073",
        comment="you can also edit the message *while the agent is starting up*",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_rsync(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    result = e2e.run(
        'mngr create my-task --type claude --provider modal --rsync --rsync-args "--exclude=node_modules" --no-connect --no-ensure-clean',
        comment="you can use rsync to transfer extra data as well",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


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
        'mngr create my-task --provider modal --type claude --no-connect --no-ensure-clean -- --dangerously-skip-permissions --append-system-prompt "Don\'t ask me any questions!"',
        comment="one of the coolest features of mngr is the ability to create agents on remote hosts",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


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
    # The tutorial command omits --type because it assumes the user has a default
    # agent type configured (via `mngr extras config` / `mngr config set
    # commands.create.type ...`). The e2e environment is intentionally fresh and
    # has no such default, so we pass --type claude explicitly here.
    result = e2e.run(
        "mngr create my-task --type claude --provider modal --idle-timeout 60 --no-connect --no-ensure-clean",
        comment="mngr makes it really easy to deal with this by automatically shutting down hosts when their agents are idle",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent was actually created with idle_timeout_seconds=60 -- a
    # bare exit-code check doesn't catch a regression where --idle-timeout
    # silently drops the value.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify idle-timeout was applied to the created agent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}: {agents}"
    assert matching[0]["idle_timeout_seconds"] == 60, (
        f"Expected idle_timeout_seconds=60, got {matching[0].get('idle_timeout_seconds')!r}"
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
    # --type command + sleep avoids needing a default agent type configured
    # in the test environment; --idle-mode "ssh" is agent-type-independent.
    result = e2e.run(
        'mngr create my-task --provider modal --idle-mode "ssh" --type command --no-connect --no-ensure-clean -- sleep 100124',
        comment="You can customize what inactivity means by using the --idle-mode flag",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.timeout(60)
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    # The e2e fixture does not configure a default [commands.create] type, so
    # an explicit --type is needed to reach the host-lookup step we want to
    # exercise. Use 'command' (a no-op type) since the agent never actually
    # starts -- the host doesn't exist, so resolution fails before that.
    result = e2e.run(
        "mngr create my-task@my-dev-box --type command --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
    )
    expect(result).to_fail()
    # The error should mention the host not being found
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)host.*not found|no.*host|unknown.*host|could not find.*host|not.*registered")
    # The error should specifically reference the requested host name so the
    # user knows which host failed to resolve.
    expect(combined).to_contain("my-dev-box")

    # Verify no agent was actually created (creation should have aborted before
    # any state was written).
    list_result = e2e.run("mngr list --format json", comment="verify no agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agent_names = [a["name"] for a in parsed["agents"]]
    assert "my-task" not in agent_names, f"Expected no my-task agent, but found: {agent_names}"


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
        "mngr create my-task --provider modal --type claude -b cpu=4 -b memory=16 -b image=python:3.12 --no-connect --no-ensure-clean",
        comment="build arguments let you customize that new remote host",
        timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
    )
    expect(result).to_succeed()

    # Verify the build args actually took effect: agent is on a Modal host
    # with the requested CPU and memory. (The image build arg is not
    # round-tripped through mngr list -- Modal does not report the base
    # image back -- so it cannot be asserted on here.)
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the build args were applied to the new Modal host",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {parsed['agents']}"
    agent = matching[0]
    host = agent["host"]
    assert host["provider_name"] == "modal", f"Expected modal provider, got: {host['provider_name']}"
    resource = host.get("resource")
    assert resource is not None, f"Expected resource info on host, got: {host}"
    assert resource["cpu"]["count"] == 4, f"Expected 4 CPUs, got: {resource['cpu']}"
    assert resource["memory_gb"] == 16, f"Expected 16 GB memory, got: {resource['memory_gb']}"


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
        "mngr create my-task --provider modal --type command -b file=./Dockerfile.agent -b context-dir=./agent-context --no-connect --no-ensure-clean -- sleep 99999",
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
    result = e2e.run(
        "mngr create my-task@my-modal-box.modal --type claude --new-host --no-connect --no-ensure-clean",
        comment="you can name the host using the address syntax",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the agent landed on a host named my-modal-box",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one my-task agent, got: {parsed['agents']}"
    agent = matching[0]
    assert agent["host"]["name"] == "my-modal-box", (
        f"Expected host name 'my-modal-box' from address syntax, got: {agent['host']['name']}"
    )
    assert agent["host"]["provider_name"] == "modal", (
        f"Expected provider 'modal' from address syntax, got: {agent['host']['provider_name']}"
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
    # The tutorial command assumes the user has set a default agent type (via
    # `mngr extras config` during install). The e2e fixture starts from a fresh
    # profile with no default, so configure one here -- using `command` with a
    # `sleep` so the agent stays alive without requiring claude/codex installed.
    expect(
        e2e.run(
            "mngr config set commands.create.type command",
            comment="configure default agent type (normally set during install)",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr config set agent_types.command.command 'sleep 99999'",
            comment="provide a default command so the command agent has something to run",
        )
    ).to_succeed()
    result = e2e.run(
        "mngr create my-task --provider modal -b volume=my-data:/data --no-connect --no-ensure-clean",
        comment="you can mount persistent Modal volumes",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


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
        "mngr create my-task@.modal:/workspace --type command --no-connect --no-ensure-clean -- sleep 100",
        comment="you can specify the target path where the agent's work directory will be mounted",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the agent's work_dir was actually mounted at the requested
    # target path, not just that the create command exited 0.
    list_result = e2e.run("mngr list --format json", comment="verify target path applied to work_dir")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected one my-task agent, got: {agents}"
    work_dir = matching[0]["work_dir"]
    assert work_dir.startswith("/workspace"), f"Expected work_dir under /workspace, got: {work_dir}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_upload_and_extra_provision_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can upload files and run custom commands during host provisioning:
    mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo"
    # (--sudo-command runs as root)
    """)
    # Create ~/.ssh/config so the upload-file flag has a real file to work with
    e2e.run("mkdir -p ~/.ssh && touch ~/.ssh/config", comment="create ssh config for upload test")
    # The tutorial block assumes the user has configured a default agent type
    # (via `mngr config set commands.create.type ...`, prompted by install.sh).
    # The isolated test profile doesn't set one, so pass --type explicitly.
    result = e2e.run(
        'mngr create my-task --provider modal --type command --upload-file ~/.ssh/config:/root/.ssh/config'
        ' --extra-provision-command "echo provisioned" --no-connect --no-ensure-clean -- sleep 100101',
        comment="you can upload files and run custom commands during host provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


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

    # Verify --no-start-on-boot actually took effect on the created agent.
    list_result = e2e.run(
        "mngr list --format json",
        comment="confirm agent was created with start_on_boot=False",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got {len(matching)}: {agents}"
    assert matching[0]["start_on_boot"] is False, (
        f"Expected start_on_boot=False after --no-start-on-boot, got {matching[0]['start_on_boot']!r}"
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

    # Verify --pass-host-env actually forwarded MY_VAR=hello to the host env
    # file (Host.set_env_vars writes to $MNGR_HOST_DIR/env).
    env_result = e2e.run(
        'mngr exec my-task \'cat "$MNGR_HOST_DIR/env"\'',
        comment="verify MY_VAR=hello was recorded in the host env file",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(env_result).to_succeed()
    assert "MY_VAR=hello" in env_result.stdout, (
        f"Expected MY_VAR=hello in host env file, got:\n{env_result.stdout}"
    )


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
    result = e2e.run(
        "mngr create sisyphus --reuse --provider modal --type claude --no-connect --no-ensure-clean",
        comment="another handy trick is to make the create command idempotent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


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
    # (rather than just running a vanilla create).
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
        "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100078",
        comment="retry settings are configured via [retry] in settings.toml",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
