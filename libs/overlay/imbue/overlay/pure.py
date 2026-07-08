from collections.abc import Callable
from typing import TypeVar

_F = TypeVar("_F", bound=Callable[..., object])


def pure(func: _F) -> _F:
    """Mark a function as pure (no side effects).

    This decorator is advisory only and is not enforced at runtime.
    It serves as documentation to indicate that the decorated function:
    - Has no side effects
    - Does not modify any state outside its scope
    - Does not perform I/O operations
    - Returns the same output for the same inputs

    Example usage:
        @pure
        def add(a: int, b: int) -> int:
            return a + b
    """
    return func
