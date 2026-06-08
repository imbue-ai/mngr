from threading import Event
from threading import Thread

from imbue.concurrency_group.event_utils import CompoundEvent
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.thread_utils import ObservableThread


def test_shutdown_event_can_be_waited_from_multiple_threads() -> None:
    shutdown_event = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(shutdown_event)

    threads = [ObservableThread(target=lambda: child.wait()) for _ in range(4)]
    for thread in threads:
        thread.start()
    shutdown_event.set()
    for thread in threads:
        thread.join()
        thread.maybe_raise()


def test_shutdown_event_wait_returns_false_on_timeout() -> None:
    """Test that wait() returns False when the timeout expires without the event being set."""
    shutdown_event = ShutdownEvent.build_root()
    result = shutdown_event.wait(timeout=0.02)
    assert result is False


def test_shutdown_event_wait_returns_true_when_external_event_is_set() -> None:
    """Test that wait() returns True when the external event is set."""
    parent = ShutdownEvent.build_root()
    external = Event()
    child = ShutdownEvent.from_parent(parent, external=external)

    # Set the external event in another thread after a short delay
    def set_external() -> None:
        external.set()

    thread = Thread(target=set_external)
    thread.start()
    result = child.wait(timeout=1.0)
    thread.join()
    assert result is True


def test_shutdown_event_wait_returns_true_when_parent_event_is_set() -> None:
    """Test that wait() returns True when the parent event is set."""
    parent = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(parent)

    # Set the parent event in another thread after a short delay
    def set_parent() -> None:
        parent.set()

    thread = Thread(target=set_parent)
    thread.start()
    result = child.wait(timeout=1.0)
    thread.join()
    assert result is True


def test_shutdown_event_is_set_via_external_event() -> None:
    """Test that is_set() returns True when the external event is set."""
    parent = ShutdownEvent.build_root()
    external = Event()
    child = ShutdownEvent.from_parent(parent, external=external)

    assert child.is_set() is False
    external.set()
    assert child.is_set() is True


def test_shutdown_event_is_set_via_parent_event() -> None:
    """Test that is_set() returns True when the parent event is set."""
    parent = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(parent)

    assert child.is_set() is False
    parent.set()
    assert child.is_set() is True


def test_shutdown_event_is_set_via_own_event() -> None:
    """Test that is_set() returns True when the own event is set."""
    shutdown_event = ShutdownEvent.build_root()

    assert shutdown_event.is_set() is False
    shutdown_event.set()
    assert shutdown_event.is_set() is True


def test_compound_event_is_set_returns_true_when_any_child_is_set() -> None:
    """Test that CompoundEvent.is_set() returns True if any child event is set."""
    event1 = Event()
    event2 = Event()
    event3 = Event()

    compound = CompoundEvent([event1, event2, event3])
    assert compound.is_set() is False

    event2.set()
    assert compound.is_set() is True


def test_compound_event_is_set_returns_false_when_no_child_is_set() -> None:
    """Test that CompoundEvent.is_set() returns False if no child event is set."""
    event1 = Event()
    event2 = Event()

    compound = CompoundEvent([event1, event2])
    assert compound.is_set() is False


def test_compound_event_wait_returns_true_when_child_is_set() -> None:
    """Test that CompoundEvent.wait() returns True when a child event is set."""
    event1 = Event()
    event2 = Event()
    compound = CompoundEvent([event1, event2])

    # Set one of the events in another thread
    def set_event() -> None:
        event1.set()

    thread = Thread(target=set_event)
    thread.start()
    result = compound.wait(timeout=1.0)
    thread.join()
    assert result is True


def test_compound_event_wait_returns_false_on_timeout() -> None:
    """Test that CompoundEvent.wait() returns False when timeout expires without any event set."""
    event1 = Event()
    event2 = Event()
    compound = CompoundEvent([event1, event2])

    result = compound.wait(timeout=0.02)
    assert result is False
