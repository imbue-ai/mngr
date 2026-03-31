import threading
from typing import Any
from typing import Callable

from loguru import logger


def _is_match_for_enumerated_exceptions(
    exception_or_exception_group: BaseException | ExceptionGroup,
    enumerated_exceptions: tuple[type[BaseException], ...],
) -> bool:
    """
    Return True if:
        - we get a single exception and it is an instance of one of the enumerated exceptions
        - or we get an ExceptionGroup and all of its contained exceptions are instances of one of the enumerated
          exceptions
    """
    if not isinstance(exception_or_exception_group, ExceptionGroup):
        return isinstance(exception_or_exception_group, enumerated_exceptions)
    return all(
        _is_match_for_enumerated_exceptions(e, enumerated_exceptions) for e in exception_or_exception_group.exceptions
    )


class ObservableThread(threading.Thread):
    """Thread that captures exceptions and returns results."""

    def __init__(
        self,
        target: Callable[..., Any],
        args: tuple = (),
        kwargs: dict | None = None,
        name: str | None = None,
        daemon: bool = True,
        silenced_exceptions: tuple[type[BaseException], ...] | None = None,
        suppressed_exceptions: tuple[type[BaseException], ...] | None = None,
        on_failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Initialize ObservableThread."""
        super().__init__(name=name, daemon=daemon)
        self._target = target
        self._target_name = getattr(target, "__name__", None) if target else None
        self._args = args
        self._kwargs = kwargs or {}
        self._exception: BaseException | None = None
        self._silenced_exceptions = silenced_exceptions or ()
        self._suppressed_exceptions = suppressed_exceptions or ()
        self._on_failure = on_failure

    @property
    def target_name(self) -> str | None:
        return self._target_name

    def run(self) -> None:
        """Run the target function."""
        try:
            super().run()
        except BaseException as e:
            self._exception = e
            if _is_match_for_enumerated_exceptions(e, self._silenced_exceptions):
                return
            else:
                logger.opt(exception=e).error(
                    "Error in thread '{}' with target '{}'",
                    self.name,
                    self.target_name,
                )
                if self._on_failure:
                    self._on_failure(e)
                raise

    def join(self, timeout: float | None = None) -> None:
        """Wait for thread completion and raise exception if any."""
        super().join(timeout)
        self.maybe_raise()

    def maybe_raise(self) -> None:
        exception = self.exception_if_not_suppressed
        if exception:
            raise exception

    @property
    def exception_raw(self) -> BaseException | None:
        """Get the exception raised in the thread if any (without re-raising)."""
        return self._exception

    @property
    def exception_if_not_suppressed(self) -> BaseException | None:
        """Get the exception raised in the thread if any, unless it is in the suppressed_exceptions list."""
        if self._exception and not _is_match_for_enumerated_exceptions(self._exception, self._suppressed_exceptions):
            return self._exception
        return None
