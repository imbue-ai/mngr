"""Tests for Docker agent creation from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block. This makes it
easy to maintain the mapping between tutorial content and test coverage via the
tutorial_matcher script.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

_REMOTE_TIMEOUT = 120.0
# A custom Dockerfile based on a bare distro image (rather than mngr's default
# image, which pre-installs everything) forces mngr to install its required
# host packages (openssh-server, tmux, rsync, git, ...) at container start.
# That runtime apt-get install plus the base image pull pushes the create past
# the default _REMOTE_TIMEOUT, so the custom-Dockerfile test gets a longer wait.
_REMOTE_TIMEOUT_CUSTOM_IMAGE = 300.0


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_create_docker_start_args(e2e: E2eSession) -> None:
    # `--gpus all` is the canonical tutorial example, but the test itself
    # exercises start-args forwarding with a flag that does not require a
    # GPU to be present in the sandbox. Modal offload sandboxes do not ship
    # the nvidia-container-runtime, so docker run would reject `--gpus all`
    # with "could not select device driver" and this test would spuriously
    # fail in no-GPU environments. `--hostname=...` is accepted by docker
    # run everywhere and proves the same thing (mngr threads `-s` arguments
    # through to `docker run`), with the added benefit that the post-create
    # `mngr exec my-task hostname` call below can read back the value and
    # confirm the arg actually took effect.
    e2e.write_tutorial_block("""
        # pass Docker-specific start args (eg, GPU access) "start args" are the args to "docker run", see "docker run --help" for all of them
        mngr create my-task --provider docker -s "--gpus all"
    """)
    # The tutorial relies on a configured default agent type (claude); the
    # isolated test profile has none, so pass `--type command -- sleep ...` as
    # a stand-in (the same convention used across the e2e tutorial tests) so
    # the test doesn't need claude installed in the container image.
    result = e2e.run(
        'mngr create my-task --provider docker -s "--hostname=mngr-start-arg-test" --type command --no-connect --no-ensure-clean -- sleep 100073',
        comment="some providers (like docker), take start args as well as build args",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()

    # Verify the start arg was passed through to docker run
    hostname_result = e2e.run(
        "mngr exec my-task hostname",
        comment="verify start arg was applied to the container",
    )
    expect(hostname_result).to_succeed()
    expect(hostname_result.stdout).to_contain("mngr-start-arg-test")


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_docker_default_image(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run an agent in a local Docker container. Will default to mngr's default image if you don't specify one.
        mngr create my-task --provider docker
    """)
    # The tutorial relies on a configured default agent type; the test
    # environment has none, so use `--type command -- sleep N` (the same
    # pattern as the other create tests) to make the command runnable. The
    # default-image behavior under test is independent of the agent type.
    expect(
        e2e.run(
            "mngr create my-task --provider docker --type command --no-connect --no-ensure-clean -- sleep 100100",
            comment="local Docker container with default image",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_create_docker_custom_dockerfile(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use a custom Dockerfile for the container image. One strange thing is that you probably want to pass "-b ." because
        # that's just how docker works (it takes the context dir as the last arg)
        mngr create my-task --provider docker -b file=./Dockerfile.dev -b .
    """)
    # The Dockerfile contents are the test's own choice (the tutorial block only
    # references the file by path). It must be a Debian/Ubuntu-family base so
    # mngr can apt-get install its required host packages at container start --
    # a bare `FROM alpine` would have no apt-get and the create would fail. This
    # mirrors the Modal custom-Dockerfile test, which uses python:3.12 for the
    # same reason.
    expect(
        e2e.run("printf 'FROM debian:bookworm-slim\\n' > Dockerfile.dev", comment="write Dockerfile.dev")
    ).to_succeed()
    # `--type command -- sleep ...` stands in for the configured default agent
    # (the isolated test profile has no default type, whereas a real tutorial
    # user would have set one); --no-connect/--no-ensure-clean keep the create
    # non-interactive. The pinned sleep value makes any leaked process traceable
    # back to this test.
    expect(
        e2e.run(
            "mngr create my-task --provider docker -b file=./Dockerfile.dev -b ."
            " --type command --no-connect --no-ensure-clean -- sleep 100920",
            comment="use a custom Dockerfile for the container image",
            timeout=_REMOTE_TIMEOUT_CUSTOM_IMAGE,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_docker_volume_start_arg(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # include additional volumes for data persistence and sharing
        mngr create my-task --provider docker -s "-v /host/data:/container/data"
        # note that all docker hosts have a default volume mounted, which is used so that the host and agent information can be
        # available even when a given "host" (container) is stopped
    """)
    expect(e2e.run("mkdir -p /tmp/mngr-test-data", comment="ensure host volume source exists")).to_succeed()
    # Seed the host volume source with a marker file so we can later prove the
    # bind mount is actually shared with the container (data sharing), not just
    # that `mngr create` accepted the start arg.
    expect(
        e2e.run("echo mngr-volume-marker > /tmp/mngr-test-data/marker.txt", comment="seed host volume with a marker")
    ).to_succeed()
    expect(
        e2e.run(
            'mngr create my-task --provider docker -s "-v /tmp/mngr-test-data:/container/data" '
            "--type command --no-connect --no-ensure-clean -- sleep 100993",
            comment="include additional volumes",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the volume was actually bind-mounted: the marker written on the
    # host must be readable from inside the container at the mount target.
    marker_result = e2e.run(
        "mngr exec my-task cat /container/data/marker.txt",
        comment="verify the host volume is mounted inside the container",
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("mngr-volume-marker")


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_docker_cpus_start_arg(e2e: E2eSession) -> None:
    # The tutorial demonstrates `-s --cpus=2`; the `--cpus=N` flag is passed
    # straight through to `docker run`. As with the other docker create tests,
    # we substitute `--type command -- sleep ...` for the real default agent so
    # the test does not need a coding agent installed inside the container.
    e2e.write_tutorial_block("""
        # set resource limits via start args
        mngr create my-task --provider docker -s --cpus=2
    """)
    expect(
        e2e.run(
            'mngr create my-task --provider docker --type command -s "--cpus=2" --no-connect --no-ensure-clean -- sleep 100993',
            comment="set resource limits via start args",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # A malformed start arg (eg, the tutorial's old `cpus=2`) would be parsed by
    # `docker run` as the image name and the create above would have failed. So
    # a clean create already proves `--cpus=2` was accepted. As an extra check,
    # confirm the resulting container is actually running and execable.
    expect(
        e2e.run(
            "mngr exec my-task echo ok",
            comment="verify the container created with the start arg is running",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_list_provider_docker(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list Docker agents
        mngr list --provider docker
    """)
    # `mngr list --provider docker` only talks to the Docker daemon through the
    # in-process SDK, never the `docker` CLI binary, so on an empty environment
    # it would report "No agents found" without exercising Docker at all (and
    # the resource guard would then fail the @pytest.mark.docker test for never
    # invoking docker). Create a Docker agent first -- `mngr create` shells out
    # to the `docker` CLI for build/run -- so the listing has something real to
    # report and we can verify the agent actually shows up.
    expect(
        e2e.run(
            "mngr create my-task --provider docker --type command --no-connect --no-ensure-clean -- sleep 1000",
            comment="create a Docker agent so the list has something to show",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --provider docker", comment="list Docker agents")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_create_docker_start_args_overview(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # some providers (like docker), take "start" args as well as build args:
        mngr create my-task --provider docker -s "--gpus all"
        # these args are passed to "docker run", whereas the build args are passed to "docker build".
    """)
    # Same substitution as test_create_docker_start_args: `--gpus all` requires
    # a real GPU runtime which the offload sandbox lacks, so use a portable
    # hostname-style arg that still proves -s is forwarded to `docker run`.
    # The test profile has no default agent type, so `--type command -- sleep ...`
    # stands in for the user's configured default and keeps the container alive.
    expect(
        e2e.run(
            'mngr create my-task --provider docker -s "--hostname=mngr-overview-test" '
            "--type command --no-connect --no-ensure-clean -- sleep 100076",
            comment="docker takes start args as well as build args",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the start arg was forwarded to `docker run` (not just accepted):
    # the container's hostname should be the value passed via -s.
    hostname_result = e2e.run(
        "mngr exec my-task hostname",
        comment="confirm the start arg reached docker run",
    )
    expect(hostname_result).to_succeed()
    expect(hostname_result.stdout).to_contain("mngr-overview-test")


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_destroy_all_docker_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all docker agents (be careful!)  Useful for cleaning up while prototyping
        mngr list --include 'host.provider == "docker"' --ids | mngr destroy -f -
    """)
    # Create a Docker agent so the "destroy all" command has something to act on.
    # Without this, the command runs against an empty set and never proves that it
    # actually destroys anything.
    expect(
        e2e.run(
            "mngr create my-task --provider docker --type command --no-connect --no-ensure-clean -- sleep 100200",
            comment="create a Docker agent to be destroyed",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    # Confirm the agent is present before we destroy it.
    list_before = e2e.run("mngr list --provider docker", comment="list Docker agents before destroy")
    expect(list_before).to_succeed()
    expect(list_before.stdout).to_contain("my-task")

    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"docker\"' --ids | mngr destroy -f -",
            comment="destroy all Docker agents via filter+stdin",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the agent is actually gone after the destroy-all command.
    list_after = e2e.run("mngr list --provider docker", comment="list Docker agents after destroy")
    expect(list_after).to_succeed()
    expect(list_after.stdout).not_to_contain("my-task")
