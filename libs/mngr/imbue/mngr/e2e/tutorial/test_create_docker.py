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
    result = e2e.run(
        'mngr create my-task --provider docker -s "--hostname=mngr-start-arg-test" --no-connect --no-ensure-clean',
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
    expect(
        e2e.run(
            "mngr create my-task --provider docker --no-connect --no-ensure-clean",
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
    expect(e2e.run("printf 'FROM alpine\\n' > Dockerfile.dev", comment="write Dockerfile.dev")).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --provider docker -b file=./Dockerfile.dev -b . --no-connect --no-ensure-clean",
            comment="use a custom Dockerfile for the container image",
            timeout=_REMOTE_TIMEOUT,
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
    expect(
        e2e.run(
            'mngr create my-task --provider docker -s "-v /tmp/mngr-test-data:/container/data" --no-connect --no-ensure-clean',
            comment="include additional volumes",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_docker_cpus_start_arg(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set resource limits via start args
        mngr create my-task --provider docker -s cpus=2
    """)
    expect(
        e2e.run(
            "mngr create my-task --provider docker -s cpus=2 --no-connect --no-ensure-clean",
            comment="set resource limits via start args",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.docker
def test_list_provider_docker(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list Docker agents
        mngr list --provider docker
    """)
    expect(e2e.run("mngr list --provider docker", comment="list Docker agents")).to_succeed()


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
    expect(
        e2e.run(
            'mngr create my-task --provider docker -s "--hostname=mngr-overview-test" --no-connect --no-ensure-clean',
            comment="docker takes start args as well as build args",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.docker
def test_destroy_all_docker_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all docker agents (be careful!)  Useful for cleaning up while prototyping
        mngr list --include 'host.provider == "docker"' --ids | mngr destroy -f
    """)
    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"docker\"' --ids | mngr destroy -f",
            comment="destroy all Docker agents via filter+stdin",
        )
    ).to_succeed()
