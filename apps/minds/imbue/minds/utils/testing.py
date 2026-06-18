"""Test doubles for ``imbue.minds.utils`` helpers.

Per CLAUDE.md, this module has no test of its own; the helpers here are
exercised through the tests that import them.
"""

import threading
from collections.abc import Mapping
from collections.abc import Sequence

from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller


class RecordingMngrCaller(MngrCaller):
    """In-process :class:`MngrCaller` double: records argv and returns a canned result.

    Avoids forking a real forkserver child (which would import the full ``mngr``
    CLI), so tests stay fast and deterministic while still being able to assert
    on the argv the caller was invoked with.

    The default result models a *successful* ``mngr message`` delivery (exit 0
    with a ``message_sent`` event on stdout) so that
    :meth:`MngrMessageSender.deliver` reports success and its verify-and-retry
    loop completes on the first attempt. Tests exercising failure or retry pass
    explicit results via :attr:`result` / :attr:`results`.
    """

    result: MngrCallResult = Field(
        default_factory=lambda: MngrCallResult(
            returncode=0, stdout='{"event": "message_sent", "agent": "recorded"}\n'
        ),
        description="Result returned for each call once ``results`` is exhausted (or for every call if ``results`` is empty).",
    )
    results: tuple[MngrCallResult, ...] = Field(
        default=(),
        description=(
            "Optional per-call results returned in order; calls past the last one fall back to ``result``. "
            "Lets a test model a call that fails then succeeds on retry."
        ),
    )
    _calls: list[list[str]] = PrivateAttr(default_factory=list)
    _called_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> MngrCallResult:
        call_index = len(self._calls)
        self._calls.append(list(argv))
        self._called_event.set()
        if call_index < len(self.results):
            return self.results[call_index]
        return self.result

    @property
    def calls(self) -> list[list[str]]:
        """The argv of each recorded call (each excludes the ``mngr`` program name)."""
        return self._calls

    @property
    def called_event(self) -> threading.Event:
        """Set once at least one call has been recorded; lets tests await a background send."""
        return self._called_event
