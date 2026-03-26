"""Tests for remote agent creation (Modal/Docker) from the tutorial.

These tests verify the CLI accepts remote-provider flags. Since Modal and Docker
are disabled in the test environment, commands fail with provider-disabled errors
rather than unknown-flag errors.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block. This makes it
easy to maintain the mapping between tutorial content and test coverage via the
tutorial_matcher script.
"""

import pytest

from imbue.mng.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

_PROVIDER_ERROR_PATTERN = r"(?i)(modal|docker).*(not authorized|not enabled|disabled|not available|not installed)"


def _assert_provider_disabled(result) -> None:
    """Assert a command failed because a remote provider is disabled."""
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    expect(combined).to_match(_PROVIDER_ERROR_PATTERN)


@pytest.mark.release
def test_create_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also launch claude remotely in Modal:
    mng create my-task --provider modal
    # see more details below in "CREATING AGENTS REMOTELY" for relevant options
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --no-ensure-clean",
            comment="you can also launch claude remotely in Modal",
        )
    )


@pytest.mark.release
def test_create_modal_no_connect_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
    mng create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github"
    # here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
    # and then we also pass in an explicit message for the agent to start working on immediately
    # the message can also be specified as the contents of a file (by using --message-file instead of --message)
    """)
    _assert_provider_disabled(
        e2e.run(
            'mng create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github" --no-ensure-clean',
            comment="you can send an initial message (so you don't have to wait around)",
        )
    )


@pytest.mark.release
def test_create_modal_edit_message(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mng create my-task --provider modal --edit-message
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --edit-message --no-ensure-clean",
            comment="you can also edit the message *while the agent is starting up*",
        )
    )


@pytest.mark.release
def test_create_modal_rsync(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use rsync to transfer extra data as well, beyond just the git data:
    mng create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"
    """)
    _assert_provider_disabled(
        e2e.run(
            'mng create my-task --provider modal --rsync --rsync-args "--exclude=node_modules" --no-ensure-clean',
            comment="you can use rsync to transfer extra data as well",
        )
    )


@pytest.mark.release
def test_create_modal_passthrough_agent_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # one of the coolest features of mng is the ability to create agents on remote hosts just as easily as you can create them locally:
    mng create my-task --provider modal -- --dangerously-skip-permissions --append-system-prompt "Don't ask me any questions!"
    # that command passes the "--dangerously-skip-permissions" flag to claude because it's safe to do so:
    # agents running remotely are running in a sandboxed environment where they can't really mess anything up on their local machine (or if they do, it doesn't matter)
    # because it's running remotely, you might also want something like that system prompt (to tell it not to get blocked on you)
    """)
    _assert_provider_disabled(
        e2e.run(
            'mng create my-task --provider modal --no-ensure-clean -- --dangerously-skip-permissions --append-system-prompt "Don\'t ask me any questions!"',
            comment="one of the coolest features of mng is the ability to create agents on remote hosts",
        )
    )


@pytest.mark.release
def test_create_modal_idle_timeout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
    # one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
    # mng makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
    mng create my-task --provider modal --idle-timeout 60
    # that command shuts down the Modal host (and agent) after 1 minute of inactivity.
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --idle-timeout 60 --no-ensure-clean",
            comment="mng makes it really easy to deal with this by automatically shutting down hosts when their agents are idle",
        )
    )


@pytest.mark.release
def test_create_modal_idle_mode_ssh(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # You can customize what "inactivity" means by using the --idle-mode flag:
    mng create my-task --provider modal --idle-mode "ssh"
    # that command will only consider agents as "idle" when you are not connected to them
    # see the idle_detection.md file for more details on idle detection and timeouts
    """)
    _assert_provider_disabled(
        e2e.run(
            'mng create my-task --provider modal --idle-mode "ssh" --no-ensure-clean',
            comment="You can customize what inactivity means by using the --idle-mode flag",
        )
    )


@pytest.mark.release
def test_create_address_syntax_existing_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mng create my-task@my-dev-box
    """)
    result = e2e.run(
        "mng create my-task@my-dev-box --no-ensure-clean",
        comment="you can specify which existing host to run on using the address syntax",
    )
    expect(result).to_fail()
    # The error should mention the host not being found
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)host.*not found|no.*host|unknown.*host|could not find.*host|not.*registered")


@pytest.mark.release
def test_create_modal_build_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # generally though, you'll want to construct a new Modal host for each agent.
    # build arguments let you customize that new remote host (eg, GPU type, memory, base Docker image for Modal):
    mng create my-task --provider modal -b cpu=4 -b memory=16 -b image=python:3.12
    # see "mng create --help" for all provider-specific build args
    # some other useful Modal build args: --region, --timeout, --offline (blocks network), --secret, --cidr-allowlist, --context-dir
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal -b cpu=4 -b memory=16 -b image=python:3.12 --no-ensure-clean",
            comment="build arguments let you customize that new remote host",
        )
    )


@pytest.mark.release
def test_create_modal_dockerfile_and_context(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # the most important build args for Modal are probably "--file" and "--context-dir",
    # which let you specify a custom Dockerfile and build context directory (respectively) for building the host environment.
    # This is how you can get custom dependencies, files, and setup steps on your Modal hosts. For example:
    mng create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context
    # that command builds a Modal host using the Dockerfile at ./Dockerfile.agent and the build context at ./agent-context
    # (which is where the Dockerfile can COPY files from, and also where build args are evaluated from)
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context --no-ensure-clean",
            comment="the most important build args for Modal are --file and --context-dir",
        )
    )


@pytest.mark.release
def test_create_named_host_new_host(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can name the host using the address syntax:
    mng create my-task@my-modal-box.modal --new-host
    # (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task@my-modal-box.modal --new-host --no-ensure-clean",
            comment="you can name the host using the address syntax",
        )
    )


@pytest.mark.release
def test_create_modal_volume(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
    mng create my-task --provider modal -b volume=my-data:/data
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal -b volume=my-data:/data --no-ensure-clean",
            comment="you can mount persistent Modal volumes",
        )
    )


@pytest.mark.release
def test_create_modal_snapshot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use an existing snapshot instead of building a new host from scratch:
    mng create my-task --provider modal --snapshot snap-123abc
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --snapshot snap-123abc --no-ensure-clean",
            comment="you can use an existing snapshot instead of building a new host from scratch",
        )
    )


@pytest.mark.release
@pytest.mark.docker
def test_create_docker_start_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # some providers (like docker), take "start" args as well as build args:
    mng create my-task --provider docker -s "--gpus all"
    # these args are passed to "docker run", whereas the build args are passed to "docker build".
    """)
    _assert_provider_disabled(
        e2e.run(
            'mng create my-task --provider docker -s "--gpus all" --no-ensure-clean',
            comment="some providers (like docker), take start args as well as build args",
        )
    )


@pytest.mark.release
def test_create_modal_target_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the target path where the agent's work directory will be mounted:
    mng create my-task --provider modal --target-path /workspace
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --target-path /workspace --no-ensure-clean",
            comment="you can specify the target path where the agent's work directory will be mounted",
        )
    )


@pytest.mark.release
def test_create_modal_upload_and_extra_provision_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can upload files and run custom commands during host provisioning:
    mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo"
    # (--append-to-file and --prepend-to-file are also available)
    """)
    _assert_provider_disabled(
        e2e.run(
            'mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "pip install foo" --no-ensure-clean',
            comment="you can upload files and run custom commands during host provisioning",
        )
    )


@pytest.mark.release
def test_create_modal_no_start_on_boot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, agents are started when a host is booted. This can be disabled:
    mng create my-task --provider modal --no-start-on-boot
    # but it only makes sense to do this if you are running multiple agents on the same host
    # that's because hosts are automatically stopped when they have no more running agents, so you have to have at least one.
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --no-start-on-boot --no-ensure-clean",
            comment="by default, agents are started when a host is booted; this can be disabled",
        )
    )


@pytest.mark.release
def test_create_modal_pass_host_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also set host-level environment variables (separate from agent env vars):
    mng create my-task --provider modal --pass-host-env MY_VAR
    # --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)
    """)
    _assert_provider_disabled(
        e2e.run(
            "MY_VAR=hello mng create my-task --provider modal --pass-host-env MY_VAR --no-ensure-clean",
            comment="you can also set host-level environment variables",
        )
    )


@pytest.mark.release
def test_create_modal_reuse(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mng create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create sisyphus --reuse --provider modal --no-ensure-clean",
            comment="another handy trick is to make the create command idempotent",
        )
    )


@pytest.mark.release
def test_create_modal_retry(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control connection retries and timeouts:
    mng create my-task --provider modal --retry 5 --retry-delay 10s
    # (--reconnect / --no-reconnect controls auto-reconnect on disconnect)
    """)
    _assert_provider_disabled(
        e2e.run(
            "mng create my-task --provider modal --retry 5 --retry-delay 10s --no-ensure-clean",
            comment="you can control connection retries and timeouts",
        )
    )
