import threading

from pydantic import Field
from pydantic import PrivateAttr

from imbue.minds.desktop_client.provider_relay import SignInCallbackForwarderInterface
from imbue.minds.desktop_client.provider_relay import SignInCallbackRefusedError
from imbue.minds.desktop_client.provider_relay import SignInCallbackResult
from imbue.minds.desktop_client.provider_relay import SignInForwardError


class RecordingSignInCallbackForwarder(SignInCallbackForwarderInterface):
    """Records every relayed callback and answers with a fixed result, or fails or is refused once told to.

    With ``is_holding`` set, each forward waits for ``release_held`` first, so a test can hold a
    callback in flight.
    """

    result: SignInCallbackResult = Field(description="What every forward answers")
    is_failing: bool = Field(default=False, description="Whether forwards raise SignInForwardError")
    is_refusing: bool = Field(default=False, description="Whether forwards raise SignInCallbackRefusedError")
    is_holding: bool = Field(default=False, description="Whether forwards wait for release_held before answering")

    _forwarded: list[str] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _released: threading.Event = PrivateAttr(default_factory=threading.Event)

    def forward(self, path_and_query: str) -> SignInCallbackResult:
        with self._lock:
            self._forwarded.append(path_and_query)
        if self.is_holding:
            self._released.wait(timeout=30.0)
        if self.is_failing:
            raise SignInForwardError("the workspace did not answer")
        if self.is_refusing:
            raise SignInCallbackRefusedError("that sign-in is no longer active")
        return self.result

    def forwarded(self) -> list[str]:
        with self._lock:
            return list(self._forwarded)

    def release_held(self) -> None:
        self._released.set()
