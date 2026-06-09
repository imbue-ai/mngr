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
# Building a custom image (pull debian:bookworm-slim + apt-get install) is far
# slower than launching a pre-built default image, so give it a wider budget.
_BUILD_TIMEOUT = 480.0


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
        "mngr create my-task --provider docker --type command"
        ' -s "--hostname=mngr-start-arg-test" --no-connect --no-ensure-clean -- sleep 100605',
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
    # The tutorial relies on a configured default agent type ([commands.create]
    # type in user settings); the isolated e2e profile has none, so make the
    # type explicit with `--type command` and give it a long sleep as the agent
    # process (the same stand-in used by the basic-creation tutorial tests).
    expect(
        e2e.run(
            "mngr create my-task --provider docker --type command --no-connect --no-ensure-clean -- sleep 100073",
            comment="local Docker container with default image",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Confirm the agent really landed on the docker provider, not just that the
    # command exited 0.
    docker_agents = e2e.run(
        "mngr list --include 'host.provider == \"docker\"'",
        comment="confirm the agent is running on the docker provider",
    )
    expect(docker_agents).to_succeed()
    expect(docker_agents.stdout).to_contain("my-task")

    # The whole point of this block is that no image was specified, so mngr's
    # default image (debian:bookworm-slim) must be the one running. Read the
    # container's /etc/os-release to prove the default image was used -- this is
    # how a human would verify it interactively.
    os_release = e2e.run(
        "mngr exec my-task cat /etc/os-release",
        comment="verify the default image (debian:bookworm-slim) is in use",
    )
    expect(os_release).to_succeed()
    expect(os_release.stdout).to_contain("bookworm")


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
    # The custom base image must provide the packages mngr installs on every
    # host (openssh-server, tmux, python3, rsync). `alpine` lacks apt-get, so
    # the runtime package install would fail; `debian:bookworm-slim` provides
    # apt-get and lets us pre-bake the packages, mirroring the proven pattern in
    # providers/docker/test_docker_create.py::test_mngr_create_with_dockerfile_on_docker.
    # Bake a marker file into the image so we can later prove the container was
    # built from *this* Dockerfile rather than mngr's default image.
    expect(
        e2e.run(
            "printf 'FROM debian:bookworm-slim\\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "openssh-server tmux python3 rsync && rm -rf /var/lib/apt/lists/*\\n"
            "RUN echo custom-dockerfile-marker > /dockerfile-marker.txt\\n' > Dockerfile.dev",
            comment="write Dockerfile.dev",
        )
    ).to_succeed()
    expect(
        e2e.run(
            # `--type command -- sleep ...` keeps the agent lightweight (the
            # tutorial assumes a default agent type is configured; the e2e
            # environment has none, so an explicit type is required).
            "mngr create my-task --provider docker --type command "
            "-b file=./Dockerfile.dev -b . --no-connect --no-ensure-clean -- sleep 100930",
            comment="use a custom Dockerfile for the container image",
            timeout=_BUILD_TIMEOUT,
        )
    ).to_succeed()

    # Verify the container actually runs the custom image: the marker file only
    # exists because our Dockerfile created it. The default image would not have
    # it, so this distinguishes "custom Dockerfile was used" from "create just
    # happened to succeed against the default image".
    marker_result = e2e.run(
        "mngr exec my-task cat /dockerfile-marker.txt",
        comment="verify the custom Dockerfile was used to build the image",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("custom-dockerfile-marker")


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
    # Drop a sentinel file on the host side of the volume so we can confirm the
    # bind mount actually exposes the host directory inside the container.
    expect(
        e2e.run(
            "echo volume-mount-works > /tmp/mngr-test-data/sentinel.txt",
            comment="write a sentinel file on the host side of the volume",
        )
    ).to_succeed()
    # `--type command -- sleep <N>` stands in for the user's configured default
    # agent type (the tutorial assumes one is set); see test_create_basic.py.
    expect(
        e2e.run(
            'mngr create my-task --provider docker -s "-v /tmp/mngr-test-data:/container/data"'
            " --type command --no-connect --no-ensure-clean -- sleep 100080",
            comment="include additional volumes",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the start arg actually mounted the host volume: the sentinel file
    # written on the host must be readable from inside the container at the
    # mount target.
    read_result = e2e.run(
        "mngr exec my-task cat /container/data/sentinel.txt",
        comment="verify the host volume is mounted inside the container",
    )
    expect(read_result).to_succeed()
    expect(read_result.stdout).to_contain("volume-mount-works")


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_create_docker_cpus_start_arg(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # set resource limits via start args
        mngr create my-task --provider docker -s --cpus=2
    """)
    # The tutorial block omits an agent type because it assumes the user has
    # configured a default (via `mngr extras config`). The e2e environment has
    # no default, so we pass `--type command -- sleep ...` as a stand-in, the
    # same convention used by the other create tutorial tests.
    expect(
        e2e.run(
            "mngr create my-task --provider docker -s --cpus=2 --type command "
            "--no-connect --no-ensure-clean -- sleep 100100",
            comment="set resource limits via start args",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Verify the CPU limit was actually applied inside the container, not just
    # that create exited 0. `--cpus=2` sets a CFS quota of 200000us (2x the
    # 100000us period), readable via the cgroup interface: cgroup v2 exposes it
    # at cpu.max ("200000 100000") and cgroup v1 at cpu.cfs_quota_us ("200000").
    # Both forms contain "200000".
    cpu_limit_result = e2e.run(
        "mngr exec my-task -- sh -c "
        "'cat /sys/fs/cgroup/cpu.max 2>/dev/null "
        "|| cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null'",
        comment="verify the CPU limit was applied to the container",
    )
    expect(cpu_limit_result).to_succeed()
    expect(cpu_limit_result.stdout).to_contain("200000")


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_list_provider_docker(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list Docker agents
        mngr list --provider docker
    """)
    # Create a Docker agent first so the listing has something real to show. This also
    # makes the test exercise the docker provider end to end: `mngr list` alone reads the
    # state volume via the docker SDK in a subprocess, which the @pytest.mark.docker CLI
    # guard cannot observe, so without a create the mark would be flagged as never invoked.
    # Use a lightweight `--type command -- sleep ...` agent (as the basic-create tests do)
    # so the test does not pay claude-startup time on every run.
    expect(
        e2e.run(
            "mngr create my-task --provider docker --type command --no-connect --no-ensure-clean -- sleep 100200",
            comment="create a Docker agent so the listing is non-empty",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    result = e2e.run("mngr list --provider docker", comment="list Docker agents")
    expect(result).to_succeed()
    # The agent we just created must appear in the docker-provider listing.
    expect(result.stdout).to_contain("my-task")


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
    # `--type command -- sleep ...` supplies the agent type the tutorial assumes
    # is configured as a default (the e2e env has none) and keeps the container
    # alive so the start arg can be read back below.
    expect(
        e2e.run(
            'mngr create my-task --provider docker --type command -s "--hostname=mngr-overview-test" --no-connect --no-ensure-clean -- sleep 100205',
            comment="docker takes start args as well as build args",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Don't stop at exit code 0: prove the -s start arg was actually handed to
    # `docker run` (and not silently dropped) by reading the hostname back out
    # of the running container.
    hostname_result = e2e.run(
        "mngr exec my-task hostname",
        comment="verify the start arg was forwarded to docker run",
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
    # Create a real Docker agent so the filter+stdin destroy actually has
    # something to remove. Without this, the command would run against an empty
    # agent list and prove nothing about whether destroy works.
    expect(
        e2e.run(
            "mngr create my-task --provider docker --type command --no-connect --no-ensure-clean -- sleep 100250",
            comment="create a Docker agent to be destroyed",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()

    # Confirm the agent is present and matched by the docker provider filter
    # before we destroy it.
    list_before = e2e.run("mngr list --provider docker", comment="list Docker agents before destroy")
    expect(list_before).to_succeed()
    expect(list_before.stdout).to_contain("my-task")

    # Run the tutorial command: select all docker agents by filter and destroy
    # them by piping their ids into `mngr destroy`.
    expect(
        e2e.run(
            "mngr list --include 'host.provider == \"docker\"' --ids | mngr destroy -f -",
            comment="destroy all Docker agents via filter+stdin",
        )
    ).to_succeed()

    # Verify the concrete effect: the agent is gone from the docker listing.
    list_after = e2e.run("mngr list --provider docker", comment="list Docker agents after destroy")
    expect(list_after).to_succeed()
    expect(list_after.stdout).not_to_contain("my-task")
