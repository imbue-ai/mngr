"""Tests for Modal agent creation from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block. This makes it
easy to maintain the mapping between tutorial content and test coverage via the
tutorial_matcher script.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

_REMOTE_TIMEOUT = 120.0

# Note: @pytest.mark.modal is NOT used here. In libs/mngr/conftest.py, the
# modal resource guard is a PATH wrapper for the `modal` CLI binary. These
# e2e tests run mngr as a subprocess, which uses the Modal Python SDK (not
# the `modal` CLI), so the PATH wrapper never fires; adding the mark would
# cause "Test marked with @pytest.mark.modal but never invoked modal" failures.
# The @pytest.mark.rsync mark IS valid for tests that create Modal agents,
# because the rsync guard uses a PATH wrapper script that subprocesses inherit.


@pytest.mark.release
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


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_no_connect_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
    mngr create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github"
    # here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
    # and then we also pass in an explicit message for the agent to start working on immediately
    # the message can also be specified as the contents of a file (by using --message-file instead of --message)
    """)
    result = e2e.run(
        'mngr create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github" --no-ensure-clean',
        comment="you can send an initial message (so you don't have to wait around)",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_edit_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mngr create my-task --provider modal --edit-message
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --edit-message --no-connect --no-ensure-clean",
        comment="you can also edit the message *while the agent is starting up*",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_rsync(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    result = e2e.run(
        'mngr create my-task --provider modal --rsync --rsync-args "--exclude=node_modules" --no-connect --no-ensure-clean',
        comment="you can use rsync to transfer extra data as well",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
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


@pytest.mark.release
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


@pytest.mark.release
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


@pytest.mark.release
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mngr create my-task@my-dev-box
    """)
    result = e2e.run(
        "mngr create my-task@my-dev-box --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
    )
    expect(result).to_fail()
    # The error should mention the host not being found
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)host.*not found|no.*host|unknown.*host|could not find.*host|not.*registered")


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
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
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_dockerfile_and_context(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # the most important build args for Modal are probably "--file" and "--context-dir",
    # which let you specify a custom Dockerfile and build context directory (respectively) for building the host environment.
    # This is how you can get custom dependencies, files, and setup steps on your Modal hosts. For example:
    mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context
    # that command builds a Modal host using the Dockerfile at ./Dockerfile.agent and the build context at ./agent-context
    # (which is where the Dockerfile can COPY files from, and also where build args are evaluated from)
    """)
    result = e2e.run(
        "mngr create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context --no-connect --no-ensure-clean",
        comment="the most important build args for Modal are --file and --context-dir",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
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


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
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


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_snapshot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use an existing snapshot instead of building a new host from scratch:
    mngr create my-task --provider modal --snapshot snap-123abc
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --snapshot snap-123abc --no-connect --no-ensure-clean",
        comment="you can use an existing snapshot instead of building a new host from scratch",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_target_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the target path where the agent's work directory will be mounted:
    mngr create my-task --provider modal --target-path /workspace
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --target-path /workspace --no-connect --no-ensure-clean",
        comment="you can specify the target path where the agent's work directory will be mounted",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_upload_and_extra_provision_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can upload files and run custom commands during host provisioning:
    mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo"
    # (--append-to-file and --prepend-to-file are also available)
    """)
    result = e2e.run(
        'mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo" --no-connect --no-ensure-clean',
        comment="you can upload files and run custom commands during host provisioning",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
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


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
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


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_reuse(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mngr create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.
    """)
    result = e2e.run(
        "mngr create sisyphus --reuse --provider modal --no-connect --no-ensure-clean",
        comment="another handy trick is to make the create command idempotent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_modal_retry(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control connection retries and timeouts:
    mngr create my-task --provider modal --retry 5 --retry-delay 10s
    # (--reconnect / --no-reconnect controls auto-reconnect on disconnect)
    """)
    result = e2e.run(
        "mngr create my-task --provider modal --retry 5 --retry-delay 10s --no-connect --no-ensure-clean",
        comment="you can control connection retries and timeouts",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
