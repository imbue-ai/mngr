class OverlayError(Exception):
    """Raised when a config patch is structurally malformed for the merge algebra.

    Covers contradictory same-layer assigns (a bare key and its ``__assign``
    twin), shape mismatches in an ``__extend`` value (e.g. a dict value where a
    list is required), and incompatible ``__extend`` marker combinations. The
    algebra is otherwise total: ``merge`` and ``finalize`` never raise for
    narrowing, only for these parse-level structural errors.
    """
