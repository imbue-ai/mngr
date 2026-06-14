class OverlayError(Exception):
    """Raised when a config patch is structurally malformed for the merge algebra.

    Covers contradictory same-layer assigns (a bare key and its ``__assign``
    twin), shape mismatches in an ``__extend`` value (e.g. a dict value where a
    list is required), and incompatible ``__extend`` marker combinations. The
    algebra is otherwise total: ``merge`` and ``finalize`` never raise for
    narrowing, only for these parse-level structural errors.
    """


class NarrowingError(OverlayError):
    """Raised by the strict node ``merge`` when a higher layer narrows a lower one.

    Carries every narrowing path detected in the combine (``self.paths``), so the
    caller sees all violations at once rather than one at a time. A narrowing is a
    ``Default`` (bare-assign) node dropping a non-empty aggregate from the layer
    below; ``Assign`` (``__assign``) nodes and ``Static*`` payloads are exempt.
    """

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        joined = ", ".join(paths)
        super().__init__(
            f"Assignment narrows the layer below at: [{joined}]. Each path's higher-precedence "
            f"value drops entries from a non-empty aggregate below it. Use '__extend' to add "
            f"without dropping, or '__assign' to replace intentionally without this error."
        )
