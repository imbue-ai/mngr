from imbue.mngr.utils.read_deadline import _MIN_READ_TIMEOUT_SECONDS
from imbue.mngr.utils.read_deadline import reads_bounded_for
from imbue.mngr.utils.read_deadline import remaining_read_timeout


def test_remaining_read_timeout_is_passthrough_with_no_budget() -> None:
    assert remaining_read_timeout(None) is None
    assert remaining_read_timeout(5.0) == 5.0


def test_remaining_read_timeout_clamps_to_the_remaining_budget() -> None:
    with reads_bounded_for(100.0):
        remaining = remaining_read_timeout(None)
        assert remaining is not None
        assert _MIN_READ_TIMEOUT_SECONDS < remaining <= 100.0
        # A smaller explicit timeout wins; a larger one is clamped down to the budget.
        assert remaining_read_timeout(2.0) == 2.0
        larger = remaining_read_timeout(1000.0)
        assert larger is not None and larger <= 100.0


def test_remaining_read_timeout_floors_a_spent_budget() -> None:
    # A zero budget is already spent, so a read gets the floor -- never 0/None, which the
    # shell-command path would treat as "no timeout".
    with reads_bounded_for(0.0):
        assert remaining_read_timeout(None) == _MIN_READ_TIMEOUT_SECONDS
        assert remaining_read_timeout(30.0) == _MIN_READ_TIMEOUT_SECONDS


def test_reads_bounded_for_resets_on_exit() -> None:
    with reads_bounded_for(50.0):
        assert remaining_read_timeout(None) is not None
    assert remaining_read_timeout(None) is None
    assert remaining_read_timeout(7.0) == 7.0
