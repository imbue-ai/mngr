import click
import pytest

from imbue.mngr.cli.docker import DockerResizeCliOptions
from imbue.mngr.cli.docker import _build_size_request
from imbue.mngr.cli.docker import _docker_provider_names
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.local.config import LocalProviderConfig


def _make_options(cpus: int | None, memory: str | None) -> DockerResizeCliOptions:
    return DockerResizeCliOptions(
        host=HostAddress(host=HostName("my-host")),
        cpus=cpus,
        memory=memory,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )


def test_build_size_request_requires_at_least_one_dimension() -> None:
    with pytest.raises(click.UsageError, match="Nothing to resize"):
        _build_size_request(_make_options(cpus=None, memory=None))


def test_build_size_request_validates_each_dimension_before_touching_any_host() -> None:
    with pytest.raises(UserInputError, match="--cpus must be a whole number"):
        _build_size_request(_make_options(cpus=0, memory=None))
    with pytest.raises(UserInputError, match="--memory must be a docker memory size"):
        _build_size_request(_make_options(cpus=None, memory="plenty"))


def test_build_size_request_leaves_an_unset_dimension_alone() -> None:
    request = _build_size_request(_make_options(cpus=None, memory="8g"))
    assert request.cpus is None
    assert request.memory == "8g"

    both = _build_size_request(_make_options(cpus=4, memory="8g"))
    assert both.cpus == 4
    assert both.memory == "8g"


def test_docker_provider_names_keep_only_docker_backed_instances(
    temp_mngr_ctx: MngrContext, mngr_test_prefix: str
) -> None:
    config = MngrConfig(
        default_host_dir=temp_mngr_ctx.config.default_host_dir,
        prefix=mngr_test_prefix,
        providers={
            ProviderInstanceName("remote-docker"): DockerProviderConfig(isolate_host_volumes=False),
            ProviderInstanceName("my-local"): LocalProviderConfig(backend=ProviderBackendName("local")),
        },
    )
    mngr_ctx = MngrContext(config=config, pm=temp_mngr_ctx.pm, profile_dir=temp_mngr_ctx.profile_dir)

    # The test plugin manager registers only the local backend, so there is no default docker instance to list.
    assert _docker_provider_names(mngr_ctx) == ("remote-docker",)
