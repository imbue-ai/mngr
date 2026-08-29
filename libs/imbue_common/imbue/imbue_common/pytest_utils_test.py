import pytest

from imbue.imbue_common.pytest_utils import inline_snapshot_is_updating
from imbue.imbue_common.pytest_utils import is_updating_for_inline_snapshot_flags


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (None, False),
        ("", False),
        ("create", True),
        ("fix", True),
        ("report", False),
        ("update", False),
        ("report,create,update", True),
        ("report,fix", True),
        ("report,update", False),
    ],
)
def test_is_updating_for_inline_snapshot_flags_detects_create_and_fix(flags: str | None, expected: bool) -> None:
    """Only the 'create' and 'fix' flags (alone or among others) mean snapshots are being written."""
    assert is_updating_for_inline_snapshot_flags(flags) is expected


def test_inline_snapshot_is_updating_reads_inline_snapshot_config_option(request: pytest.FixtureRequest) -> None:
    """The public wrapper pulls the flag value off config.option.inline_snapshot and delegates."""
    expected = is_updating_for_inline_snapshot_flags(request.config.option.inline_snapshot)
    assert inline_snapshot_is_updating(request.config) is expected
