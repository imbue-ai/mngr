from imbue.mngr.errors import MngrError


class KanpanError(MngrError):
    """Base exception for all kanpan errors.

    Inherits `MngrError` so an uncaught instance renders as a clean ``Error: ...``
    at the CLI rather than a traceback.
    """

    ...
