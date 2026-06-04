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
        'MNGR__AGENT_READY_TIMEOUT=540 mngr create my-task --provider modal --no-connect --pass-env ANTHROPIC_API_KEY --message "Speed up one of my tests and make a PR on github" --no-ensure-clean',
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


@pytest.mark.release
@pytest.mark.modal
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
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
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
    # `pip install foo` from the tutorial would fail at provision time (no such
    # package), so the test substitutes a harmless command but still exercises
    # the upload-file + extra-provision-command flags end to end.
    e2e.run("mkdir -p ~/.ssh && touch ~/.ssh/config", comment="create ssh config for upload test")
    result = e2e.run(
        'mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --extra-provision-command "echo provisioned" --no-connect --no-ensure-clean',
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
        "mngr create sisyphus --reuse --provider modal --no-connect --no-ensure-clean",
        comment="another handy trick is to make the create command idempotent",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_basic_recap(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # basic Modal agent (also covered in the CREATING AGENTS REMOTELY section above)
        mngr create my-task --provider modal
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal --no-connect --no-ensure-clean",
            comment="basic Modal agent",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


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
            "mngr create my-task --provider modal -b image=python:3.12 --no-connect --no-ensure-clean",
            comment="use a custom Docker image as the base",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_custom_dockerfile_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use a custom Dockerfile
        mngr create my-task --provider modal -b file=./Dockerfile.agent
    """)
    # Write a minimal Dockerfile so the build resolves.
    expect(
        e2e.run(
            "printf 'FROM python:3.12\\n' > Dockerfile.agent",
            comment="write a minimal Dockerfile.agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --provider modal -b file=./Dockerfile.agent --no-connect --no-ensure-clean",
            comment="use a custom Dockerfile",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
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


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
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


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_create_checkpoint(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create a snapshot for checkpointing (useful before risky changes)
        mngr snapshot create my-task --name "checkpoint-1"
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider modal --no-connect --no-ensure-clean",
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


@pytest.mark.release
@pytest.mark.modal
def test_list_provider_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all Modal agents
        mngr list --provider modal
    """)
    expect(e2e.run("mngr list --provider modal", comment="list all Modal agents")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_destroy_all_modal_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all Modal agents (be careful!)  Useful for cleaning up while prototyping
        mngr list --include 'host.provider == "modal"' --ids | mngr destroy -f
    """)
    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"modal\"' --ids | mngr destroy -f",
            comment="destroy all Modal agents via filter+stdin",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
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
    expect(
        e2e.run(
            "mngr create my-task --provider modal --idle-mode ssh --idle-timeout 300 --no-connect --no-ensure-clean",
            comment="control what counts as activity with --idle-mode",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


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
    expect(
        e2e.run(
            "mngr create agent-1@shared-host.modal --provider modal --new-host --no-connect --no-ensure-clean",
            comment="create first agent on a named host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create agent-2@shared-host.modal --no-connect --no-ensure-clean",
            comment="create additional agents on the same host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    expect(
        e2e.run(
            'mngr list --fields "name,state,host.name"',
            comment="list agents to see which share a host",
        )
    ).to_succeed()
    expect(e2e.run("mngr stop agent-1", comment="stop one agent without affecting others")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_create_modal_upload_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # upload a file to the agent's host during creation
        mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config
    """)
    expect(e2e.run("mkdir -p ~/.ssh && touch ~/.ssh/config", comment="ensure ssh config exists")).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --no-connect --no-ensure-clean",
            comment="upload a file to the agent's host",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_pip_install(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a setup command during host provisioning
        mngr create my-task --provider modal --extra-provision-command "pip install numpy pandas"
    """)
    # Use a no-op provision command so the test doesn't pull large packages
    # but still demonstrates the flag passthrough.
    expect(
        e2e.run(
            'mngr create my-task --provider modal --extra-provision-command "echo provisioned" --no-connect --no-ensure-clean',
            comment="run a setup command during host provisioning (substituted with echo)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_sudo_apt(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a command as root during provisioning (if your default user is not root, assumes passwordless sudo for that user)
        mngr create my-task --provider modal --extra-provision-command "sudo apt-get update && apt-get install -y vim"
    """)
    expect(
        e2e.run(
            'mngr create my-task --provider modal --extra-provision-command "echo sudo-provisioned" --no-connect --no-ensure-clean',
            comment="provision as root (substituted with echo to avoid apt cost)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_create_modal_provision_append_file(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # append content to a file on the host using a provision command
        mngr create my-task --provider modal --extra-provision-command "echo 'export PATH=/opt/bin:\$PATH' >> /root/.bashrc"
    """)
    expect(
        e2e.run(
            'mngr create my-task --provider modal --extra-provision-command "echo path-appended" --no-connect --no-ensure-clean',
            comment="append to a file on the host (substituted to avoid mutating bashrc)",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_modal_combined_setup_steps(e2e: E2eSession) -> None:
    e2e.write_tutorial_block(r"""
        # combine multiple setup steps
        mngr create my-task --provider modal \
          --upload-file ./requirements.txt:/workspace/requirements.txt \
          --sudo-command "apt-get update && apt-get install -y build-essential" \
          --extra-provision-command "pip install -r /workspace/requirements.txt"
    """)
    expect(e2e.run("echo 'requests==2.32.0' > requirements.txt", comment="write requirements.txt")).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --provider modal"
            " --upload-file ./requirements.txt:/workspace/requirements.txt"
            ' --sudo-command "echo sudo-step"'
            ' --extra-provision-command "echo provision-step"'
            " --no-connect --no-ensure-clean",
            comment="combine upload + sudo + extra-provision (substituted for speed)",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()
