from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event
from threading import Lock
from weakref import WeakValueDictionary

from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel


class ShutdownEvent(MutableModel):
    """
    Encapsulate two different shutdown subevents.

    - A shutdown event that came from above (e.g. in response to a Ctrl+C signal); we're only listening to it.
    - A shutdown event initiated by the "owner" of this event.

    From the perspective of threads and processes, they don't care where the shutdown event came from.
    But we need to distinguish between them - we may want to only trigger a shutdown for a particular part of the codebase.

    This is effectively a tree of shutdown events where each node can be triggered by its parent or by itself.
    (This concept is closely related to ConcurrencyGroups which span the whole codebase in a tree structure.)
    Setting a node sets its whole subtree immediately, so waiting on any node is a single blocking wait.
    """

    _set_event: Event = PrivateAttr(default_factory=Event)
    # Guards the set transition against concurrent child and callback registration.
    _lock: Lock = PrivateAttr(default_factory=Lock)
    # Weak so that a long-lived event does not keep every child it ever had alive; keyed by
    # identity because the model itself is unhashable.
    _child_by_id: WeakValueDictionary[int, "ShutdownEvent"] = PrivateAttr(default_factory=WeakValueDictionary)
    _callbacks: list[Callable[[], None]] = PrivateAttr(default_factory=list)
    # Strong, so a live descendant keeps its whole ancestor chain alive and so stays
    # reachable from any ancestor's set().
    _parent: "ShutdownEvent | None" = PrivateAttr(default=None)

    def is_set(self) -> bool:
        return self._set_event.is_set()

    def set(self) -> None:
        # Descendants are re-set even when this event already was, so a set that was
        # interrupted partway through its subtree is finished by the next one.
        with self._lock:
            self._set_event.set()
            children = tuple(self._child_by_id.values())
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for child in children:
            child.set()
        for callback in callbacks:
            callback()

    def wait(self, timeout: float | None = None) -> bool:
        return self._set_event.wait(timeout)

    @contextmanager
    def call_on_set(self, callback: Callable[[], None]) -> Iterator[None]:
        """Run a non-blocking callback when this event is set (at once if it already is) while the block is active.

        The callback runs on whichever thread sets the event, and may still run
        shortly after the block exits if a set was already underway.
        """
        with self._lock:
            is_already_set = self._set_event.is_set()
            if not is_already_set:
                self._callbacks.append(callback)
        if is_already_set:
            callback()
        try:
            yield
        finally:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

    @classmethod
    def from_parent(cls, parent: "ShutdownEvent") -> "ShutdownEvent":
        shutdown_event = cls()
        shutdown_event._parent = parent
        with parent._lock:
            if parent._set_event.is_set():
                shutdown_event._set_event.set()
            else:
                parent._child_by_id[id(shutdown_event)] = shutdown_event
        return shutdown_event

    @classmethod
    def build_root(cls) -> "ShutdownEvent":
        return cls()


# Define some convenience type aliases.
ReadOnlyEvent = Event | ShutdownEvent
