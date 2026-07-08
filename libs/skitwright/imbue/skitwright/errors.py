class SkitwrightError(Exception):
    """Base exception for the skitwright package."""


class UnsupportedExpectTypeError(SkitwrightError, TypeError):
    """Raised when expect() is given a value of an unsupported type."""
