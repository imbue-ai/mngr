import click
import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostName
from imbue.mngr_docker.cli import DockerResizeCliOptions
from imbue.mngr_docker.cli import _build_size_request


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
