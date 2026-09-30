import traceback
from typing import Final

from imbue.imbue_common.pure import pure

# Cap on a captured traceback, in characters. Tracebacks are persisted in records a
# failure can repeat for as long as it lasts (a discovery snapshot per failing poll
# cycle, a log line per failing retry), so an unbounded string would let one outage
# bloat the file without bound. The tail is kept rather than the head: the innermost
# frames name the code that actually raised, which is the part worth having.
MAX_TRACEBACK_TEXT_LENGTH: Final[int] = 8000

_TRUNCATION_NOTICE: Final[str] = "[... traceback truncated, showing the innermost frames ...]\n"


@pure
def format_exception_traceback(exception: BaseException) -> str | None:
    """Format an exception's traceback, or None when it has none.

    An exception that was constructed but never raised carries no ``__traceback__``,
    and formatting one yields only the exception line the message already holds --
    None keeps that noise out of the record.
    """
    if exception.__traceback__ is None:
        return None
    formatted = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
    if len(formatted) <= MAX_TRACEBACK_TEXT_LENGTH:
        return formatted
    return _TRUNCATION_NOTICE + formatted[-MAX_TRACEBACK_TEXT_LENGTH:]
