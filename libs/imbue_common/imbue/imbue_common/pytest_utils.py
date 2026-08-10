import pytest

from imbue.imbue_common.pure import pure


@pure
def is_updating_for_inline_snapshot_flags(inline_snapshot_flags: str | None) -> bool:
    """Return whether the given --inline-snapshot flag value means snapshots are being written.

    ``inline_snapshot_flags`` is the raw value of ``config.option.inline_snapshot``: either
    ``None`` (the option was not passed) or a comma-separated string of flag names. Snapshots
    are being written when the ``create`` or ``fix`` flags are present.
    """
    if inline_snapshot_flags is None:
        return False

    flags = inline_snapshot_flags.split(",")
    return "create" in flags or "fix" in flags


@pure
def inline_snapshot_is_updating(config: pytest.Config) -> bool:
    """Check if inline-snapshot is running with create or fix flags.

    This is useful for tests that need to behave differently when snapshots
    are being created or fixed vs when they are being validated.
    """
    return is_updating_for_inline_snapshot_flags(config.option.inline_snapshot)
