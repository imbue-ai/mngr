from imbue.mngr.utils.error_utils import MAX_TRACEBACK_TEXT_LENGTH
from imbue.mngr.utils.error_utils import format_exception_traceback


def test_format_exception_traceback_returns_none_for_a_never_raised_exception() -> None:
    # Constructed but never raised: no __traceback__, and formatting one would add
    # only the exception line the message already carries.
    assert format_exception_traceback(FileNotFoundError(2, "No such file or directory")) is None


def test_format_exception_traceback_names_the_raising_frame() -> None:
    # The motivating case: an OSError raised without a filename stringifies to a bare
    # "[Errno 2] No such file or directory", so the traceback is the only thing that
    # can point at the code responsible.
    def _raise_bare_file_not_found() -> None:
        raise FileNotFoundError(2, "No such file or directory")

    try:
        _raise_bare_file_not_found()
    except FileNotFoundError as exc:
        formatted = format_exception_traceback(exc)

    assert formatted is not None
    assert "_raise_bare_file_not_found" in formatted
    assert "FileNotFoundError" in formatted


def test_format_exception_traceback_caps_an_oversized_traceback_and_keeps_its_tail() -> None:
    # A wedged provider writes one of these per poll cycle for as long as it stays
    # broken, so the text is capped. The tail is kept, since the innermost frames and
    # the exception line are the parts worth having.
    #
    # Oversized here via a long message rather than deep recursion: CPython collapses
    # repeated frames ("[Previous line repeated N more times]"), so even a 400-deep
    # recursion formats to a few hundred characters and would not exercise the cap.
    long_message = "x" * (MAX_TRACEBACK_TEXT_LENGTH * 2)
    try:
        raise RuntimeError(long_message)
    except RuntimeError as exc:
        formatted = format_exception_traceback(exc)

    assert formatted is not None
    assert "truncated" in formatted
    assert len(formatted) == MAX_TRACEBACK_TEXT_LENGTH + len(
        "[... traceback truncated, showing the innermost frames ...]\n"
    )
    # The tail survived: the formatted traceback ends with the exception line.
    assert formatted.rstrip().endswith("x")


def test_format_exception_traceback_leaves_a_traceback_under_the_cap_intact() -> None:
    try:
        raise RuntimeError("short and complete")
    except RuntimeError as exc:
        formatted = format_exception_traceback(exc)

    assert formatted is not None
    assert "truncated" not in formatted
    assert formatted.startswith("Traceback (most recent call last):")
