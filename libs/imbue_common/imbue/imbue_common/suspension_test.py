"""Unit tests for machine-suspension detection."""

from imbue.imbue_common.suspension import ClockReading
from imbue.imbue_common.suspension import read_clocks
from imbue.imbue_common.suspension import was_suspended_since


def _reading(wall: float, monotonic: float) -> ClockReading:
    return ClockReading(wall_seconds=wall, monotonic_seconds=monotonic)


def test_a_wall_clock_that_outran_the_monotonic_one_reads_as_suspended() -> None:
    """The signature of a lid closing: wall time passed that the process did not see."""
    stamp = _reading(wall=1000.0, monotonic=500.0)
    # 730s of wall clock, 1s of monotonic -- the shape of the reported incident.
    after_a_sleep = _reading(wall=1730.0, monotonic=501.0)

    assert was_suspended_since(stamp, now=after_a_sleep)


def test_a_process_starved_of_cpu_does_not_read_as_suspended() -> None:
    """Both clocks advance together, and a starved process kept its connections."""
    stamp = _reading(wall=1000.0, monotonic=500.0)
    after_a_stall = _reading(wall=1120.0, monotonic=620.0)

    assert not was_suspended_since(stamp, now=after_a_stall)


def test_a_wall_clock_stepped_backwards_reads_as_no_suspension() -> None:
    """An NTP correction is not evidence the machine stopped."""
    stamp = _reading(wall=1000.0, monotonic=500.0)
    after_a_correction = _reading(wall=940.0, monotonic=510.0)

    assert not was_suspended_since(stamp, now=after_a_correction)


def test_a_divergence_under_the_threshold_reads_as_no_suspension() -> None:
    """Sampling the pair non-atomically puts them slightly out of step; that is not a sleep."""
    stamp = _reading(wall=1000.0, monotonic=500.0)
    slightly_skewed = _reading(wall=1010.5, monotonic=510.0)

    assert not was_suspended_since(stamp, now=slightly_skewed)
    assert was_suspended_since(stamp, now=slightly_skewed, threshold_seconds=0.25)


def test_a_reading_compared_against_itself_reads_as_no_suspension() -> None:
    """The live clocks, taken twice in a row, must never accuse the machine of sleeping."""
    stamp = read_clocks()

    assert not was_suspended_since(stamp)
