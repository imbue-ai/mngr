from imbue.minds.bootstrap import BootstrapError


class MindsSettingsError(BootstrapError):
    """Raised when a minds-side mngr settings operation cannot proceed.

    Subclasses ``BootstrapError`` (rather than the hierarchy in ``minds.errors``)
    because this package runs before mngr is importable and ``minds.errors``
    transitively pulls in click and mngr.
    """
