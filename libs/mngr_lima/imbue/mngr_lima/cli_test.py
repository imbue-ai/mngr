import click
import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostName
from imbue.mngr_lima.cli import LimaResizeCliOptions
from imbue.mngr_lima.cli import _build_size_request


def _make_options(cpus: int | None, memory: float | None, disk: int | None) -> LimaResizeCliOptions:
    return LimaResizeCliOptions(
        host=HostAddress(host=HostName("my-host")),
        cpus=cpus,
        memory=memory,
        disk=disk,
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
        _build_size_request(_make_options(cpus=None, memory=None, disk=None))


def test_build_size_request_validates_each_dimension_before_touching_any_host() -> None:
    with pytest.raises(UserInputError, match="--cpus must be a whole number"):
        _build_size_request(_make_options(cpus=0, memory=None, disk=None))
    with pytest.raises(UserInputError, match="--memory must be a number of GiB"):
        _build_size_request(_make_options(cpus=None, memory=-1.0, disk=None))
    with pytest.raises(UserInputError, match="--disk must be a whole number of GiB"):
        _build_size_request(_make_options(cpus=None, memory=None, disk=0))


def test_build_size_request_renders_the_disk_in_lima_spelling_and_leaves_unset_dimensions_alone() -> None:
    request = _build_size_request(_make_options(cpus=None, memory=1.5, disk=200))
    assert request.cpus is None
    assert request.memory_gib == 1.5
    assert request.data_disk_size == "200GiB"

    cpus_only = _build_size_request(_make_options(cpus=4, memory=None, disk=None))
    assert cpus_only.cpus == 4
    assert cpus_only.memory_gib is None
    assert cpus_only.data_disk_size is None
