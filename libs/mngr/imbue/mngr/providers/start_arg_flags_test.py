import pytest

from imbue.mngr.providers.start_arg_flags import flag_value_at
from imbue.mngr.providers.start_arg_flags import strip_flags


@pytest.mark.parametrize(
    ("start_args", "idx", "flags", "expected"),
    [
        (("--cpus=2",), 0, ("--cpus",), ("2", 1)),
        (("--cpus", "2"), 0, ("--cpus",), ("2", 2)),
        (("-m4g",), 0, ("--memory", "-m"), ("4g", 1)),
        (("-m", "4g"), 0, ("--memory", "-m"), ("4g", 2)),
        (("--cpus",), 0, ("--cpus",), (None, 1)),
        (("--cpus-extra=2",), 0, ("--cpus",), (None, 0)),
        (("--vm-type=vz",), 0, ("--cpus", "--memory"), (None, 0)),
    ],
)
def test_flag_value_at_reads_every_spelling_and_reports_the_tokens_it_spans(
    start_args: tuple[str, ...], idx: int, flags: tuple[str, ...], expected: tuple[str | None, int]
) -> None:
    assert flag_value_at(start_args, idx, flags) == expected


def test_strip_flags_drops_every_spelling_of_the_flags_and_keeps_the_rest() -> None:
    start_args = ("--tmpfs", "/run", "--cpus", "2", "-m4g", "--memory=8g", "--workdir=/", "--cpus=4")
    assert strip_flags(start_args, ("--cpus",)) == ("--tmpfs", "/run", "-m4g", "--memory=8g", "--workdir=/")
    assert strip_flags(start_args, ("--memory", "-m")) == ("--tmpfs", "/run", "--cpus", "2", "--workdir=/", "--cpus=4")
    assert strip_flags(start_args, ()) == start_args
    assert strip_flags((), ("--cpus",)) == ()
