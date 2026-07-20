from imbue.mngr.errors import MngrError


class SpecCorpusRootNotFoundError(MngrError, FileNotFoundError):
    """Raised when the behavioral-spec corpus root passed to a scan is not an existing directory."""

    ...


class SpecValidationFailedError(MngrError):
    """Raised by ``mngr specs validate`` when the corpus has language violations (after listing them)."""

    ...


class SpecListingIncompleteError(MngrError):
    """Raised by ``mngr specs list`` when some units could not be represented as records.

    The representable records are still emitted on stdout first; this error
    (after per-problem stderr diagnostics) makes the incompleteness visible to
    pipelines via the exit code.
    """

    ...


class SpecTestsRootNotFoundError(MngrError, FileNotFoundError):
    """Raised by ``mngr specs matrix`` when a ``--tests`` path does not exist."""

    ...


class SpecWitnessCollectionError(MngrError):
    """Raised by ``mngr specs matrix`` when the inner ``pytest --collect-only`` run cannot collect.

    Exit codes 0 (items collected) and 5 (none collected) are both fine; any
    other exit code, a timeout, or unparseable plugin output is a hard failure
    that carries an excerpt of the pytest output.
    """

    ...


class SpecDanglingWitnessError(MngrError):
    """Raised by ``mngr specs matrix`` when a ``witnesses`` marker does not name a real spec unit.

    Covers a coordinate matching no corpus unit (dangling) and invalid marker
    usage (no positional coordinate, or a non-string one). The matrix records
    are still emitted on stdout first; this error (after per-marker stderr
    diagnostics) makes the broken links visible via the exit code.
    """

    ...
