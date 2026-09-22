from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Why the commands issued on the current execution context must stay out of the logs, or None
# when they may be logged in full. A ContextVar (not a thread-local) so it is correctly isolated
# per pool thread and per gevent greenlet: the scope that sets it and the host log sites that read
# it run in the same context, while every other context keeps logging its commands as usual.
_command_log_suppression_reason: ContextVar[str | None] = ContextVar(
    "mngr_command_log_suppression_reason", default=None
)


@contextmanager
def commands_kept_out_of_logs(reason: str) -> Iterator[None]:
    """Keep the text of every command a host runs on this execution context out of the logs.

    For commands whose *text* is itself a secret -- a script carrying credential material, or one
    the shell reads a key out of -- where logging it would write that secret to disk. ``reason``
    is what the logs say in the command's place, so it should name the kind of command withheld.
    Nesting replaces the reason for the inner scope.

    The guard reaches the host layer's own log sites, plus the pyinfra-to-loguru handler, which
    honors it by dropping everything pyinfra logs for the length of the scope (the SSH connector
    quotes the whole command it is about to run, and nothing finer would catch that). It reaches
    nothing else, so a caller that logs a command of its own before handing it to a host still
    logs it in full.
    """
    token = _command_log_suppression_reason.set(reason)
    try:
        yield
    finally:
        _command_log_suppression_reason.reset(token)


def is_command_logging_suppressed() -> bool:
    """Whether a ``commands_kept_out_of_logs`` scope is active on this execution context."""
    return _command_log_suppression_reason.get() is not None


def loggable_command(command: str) -> str:
    """The command itself, or -- inside a ``commands_kept_out_of_logs`` scope -- a stand-in for it.

    The stand-in keeps what a reader needs to follow the story (which kind of command ran, and how
    big it was) without any of its contents.
    """
    reason = _command_log_suppression_reason.get()
    if reason is None:
        return command
    return f"<{reason}, {len(command.encode('utf-8'))} bytes, not logged>"


def withheld_command_label(command: str) -> str | None:
    """A log-safe label for a locally spawned ``command``, or None when it may be logged in full.

    Shaped for the ``name`` of a process started through ``ConcurrencyGroup``, which otherwise
    renders the whole argv into the reader thread's name (recorded in the JSONL logs) and into
    any error raised for the process. None is that parameter's own default, so outside a
    ``commands_kept_out_of_logs`` scope nothing changes.
    """
    if not is_command_logging_suppressed():
        return None
    return loggable_command(command)
