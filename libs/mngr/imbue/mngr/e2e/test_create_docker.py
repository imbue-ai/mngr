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
    # fail in no-GPU environments. `-e MNGR_START_ARG=smoke` is accepted by
    # docker run everywhere and proves the same thing: mngr threads `-s`
    # arguments through to `docker run`.
    e2e.write_tutorial_block("""
    # some providers (like docker), take "start" args as well as build args:
    mngr create my-task --provider docker -s "--gpus all"
    # these args are passed to "docker run", whereas the build args are passed to "docker build".
    """)
    result = e2e.run(
        'mngr create my-task --provider docker -s "-e MNGR_START_ARG=smoke" --no-connect --no-ensure-clean',
        comment="some providers (like docker), take start args as well as build args",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
